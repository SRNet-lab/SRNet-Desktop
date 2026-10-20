"""~/.srnet/state.json is shared by every srnet process and the exits add-on: writes are atomic,
update() never loses another process's change, and a stale copy cannot wipe a connection."""
import json
import multiprocessing as mp
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from srnet import state  # noqa: E402


def _worker(root, i, n):
    sys.path[:0] = [str(ROOT)]
    from srnet import state as s
    st = s.State(Path(root))
    for k in range(n):
        def fn(d, k=k):
            d["n"] = d.get("n", 0) + 1
            d.setdefault("by", {})[str(i)] = k + 1
        st.update(fn)


class StateFile(unittest.TestCase):
    def test_many_processes_lose_no_update(self):
        root = tempfile.mkdtemp()
        ctx = mp.get_context("spawn")
        procs = [ctx.Process(target=_worker, args=(root, i, 40)) for i in range(12)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(120)
            self.assertEqual(p.exitcode, 0)
        d = json.loads((Path(root) / "state.json").read_text())
        self.assertEqual(d["n"], 480)
        self.assertEqual(d["by"], {str(i): 40 for i in range(12)})
        self.assertEqual([p.name for p in Path(root).iterdir() if p.name.endswith(".tmp")], [])

    def test_reader_never_sees_a_half_written_file(self):
        root = Path(tempfile.mkdtemp())
        state.State(root).update(lambda d: None)
        w = mp.get_context("spawn").Process(target=_worker, args=(str(root), 0, 300))
        w.start()
        while w.is_alive():
            json.loads((root / "state.json").read_text())
        w.join()
        self.assertEqual(w.exitcode, 0)

    def test_stale_copy_keeps_connected_and_tunnel_dns(self):
        root = Path(tempfile.mkdtemp())
        stale = state.State(root)               # e.g. the exits page, loaded before the connect
        other = state.State(root)
        conn = {"seller": "05" + "ab" * 32, "killswitch": True}
        tdns = {"saved": {"tun": "sr-tun0"}, "forwarder_pid": 4242}
        other.update(lambda d: d.update(connected=conn, tunnel_dns=tdns))
        stale.update(lambda d: d.__setitem__("pending_buy", {"id": "q1"}))
        d = json.loads((root / "state.json").read_text())
        self.assertEqual(d["connected"], conn)
        self.assertEqual(d["tunnel_dns"], tdns)
        self.assertEqual(d["pending_buy"], {"id": "q1"})
        self.assertEqual(stale.data["connected"], conn)

    def test_save_is_atomic_and_private(self):
        root = Path(tempfile.mkdtemp())
        st = state.State(root)
        st.data["x"] = 1
        st.save()
        p = root / "state.json"
        self.assertEqual(json.loads(p.read_text())["x"], 1)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
