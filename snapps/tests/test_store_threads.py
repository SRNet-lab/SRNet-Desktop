"""Regression: page loads (reads) racing a background sync (writes) on one shared Store.

Before the fix, reads on the shared sqlite connection were unlocked, so a view could get a
row with env=None ('the JSON object must be str, ... not NoneType') during a sync."""
import os, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from typed.store import Store  # noqa: E402


class Meta:
    def __init__(self, i):
        self.id, self.author, self.ref, self.ts, self.t = f"id{i}", "a" * 64, None, i, "room.msg"


class StoreThreads(unittest.TestCase):
    def test_reads_during_writes(self):
        d = tempfile.mkdtemp(prefix="store-thr-")
        s = Store(os.path.join(d, "s.db"))
        errors, stop = [], threading.Event()

        def writer():
            try:
                for i in range(1, 1501):
                    s.append({"body": {"text": "x" * 200, "n": i}}, Meta(i))
                    if i % 7 == 0:
                        s.add_record("mod", {"n": i}, f"r{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
            finally:
                stop.set()

        def reader():
            while not stop.is_set():
                try:
                    for e in s.since(0, 1 << 30):
                        assert isinstance(e["env"], dict), e
                    s.records_since(0, 1 << 30); s.head(); s.rhead()
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e)); return

        ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
        for t in ts: t.start()
        for t in ts: t.join(120)
        self.assertEqual(errors, [])
        self.assertEqual(len(s.since(0, 1 << 30)), 1500)


if __name__ == "__main__":
    unittest.main()
