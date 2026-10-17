import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.stdin = io.StringIO()
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

from srnet import state, ui  # noqa: E402

SESH_A = "a" * 52 + ".sesh"
SESH_B = "b" * 52 + ".sesh"


class FakeBackend(ui.Backend):
    def __init__(self, st):
        super().__init__(st)
        self.calls = []

    def board(self):
        return {"listings": [{"seller": SESH_A, "region": "us", "price": "0.50", "age_s": 60}],
                "seq": 3, "source": "x.sesh", "dropped": 0}

    def _srnet(self, args, passphrase, timeout=600):
        self.calls.append((args, passphrase))
        return {"ok": True, "out": "", "err": ""}


class UiTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.be = FakeBackend(state.State(self.home))
        self.srv, self.url = ui.serve(self.be, legacy=True)
        self.port = self.srv.server_address[1]
        self.token = self.url.split("k=", 1)[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, path, body, token=None, headers=None):
        h = {"Content-Type": "application/json", "X-CSRF-Token": self.token if token is None else token}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_binds_loopback_random_port(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.port, 0)
        self.assertGreaterEqual(len(self.token), 24)

    def test_page_needs_token(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/")
        self.assertEqual(cm.exception.code, 403)
        page = urllib.request.urlopen(self.url).read().decode()
        self.assertIn(self.token, page)
        self.assertNotIn("<script>", page)  # no inline script; CSP script-src 'self'

    def test_csp_and_frame_headers(self):
        r = urllib.request.urlopen(self.url)
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")

    def test_api_needs_csrf_token(self):
        code, _ = self.post("/api/board", {}, token="")
        self.assertEqual(code, 403)
        code, _ = self.post("/api/board", {}, token="wrong")
        self.assertEqual(code, 403)
        code, r = self.post("/api/board", {})
        self.assertEqual(code, 200)
        self.assertEqual(r["listings"][0]["seller"], SESH_A)

    def test_foreign_origin_and_host_refused(self):
        code, _ = self.post("/api/board", {}, headers={"Origin": "http://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.post("/api/board", {}, headers={"Host": "evil.example"})
        self.assertEqual(code, 403)

    def test_form_posts_refused(self):
        req = urllib.request.Request(self.base + "/api/board", data=b"a=b", method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "X-CSRF-Token": self.token})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 415)

    def test_buy_passes_passphrase_and_never_stores_it(self):
        code, r = self.post("/api/buy", {"seller": SESH_A, "client_sesh": SESH_B, "passphrase": "hunter2-x"})
        self.assertTrue(r["ok"], r)
        args, pw = self.be.calls[-1]
        self.assertEqual(args[:2], ["buy", SESH_A])
        self.assertIn("--hours", args)
        self.assertEqual(args[args.index("--hours") + 1], "1")
        self.assertEqual(pw, "hunter2-x")
        for f in self.home.rglob("*"):
            if f.is_file():
                self.assertNotIn(b"hunter2-x", f.read_bytes(), f)

    def test_buy_validates_input(self):
        code, r = self.post("/api/buy", {"seller": "x", "client_sesh": SESH_B, "passphrase": "p"})
        self.assertEqual(code, 400)
        code, r = self.post("/api/buy", {"seller": SESH_A, "client_sesh": SESH_B})
        self.assertEqual(code, 400)

    def test_fund_flag(self):
        self.post("/api/buy", {"seller": SESH_A, "client_sesh": SESH_B, "passphrase": "p", "fund": True})
        self.assertIn("--fund", self.be.calls[-1][0])

    def test_connect_disconnect(self):
        self.post("/api/connect", {"seller": SESH_A, "client_sesh": SESH_B, "passphrase": "p"})
        self.assertEqual(self.be.calls[-1][0][:2], ["connect", SESH_A])
        self.post("/api/disconnect", {})
        self.assertEqual(self.be.calls[-1], (["disconnect"], None))

    def test_autorenew(self):
        code, r = self.post("/api/autorenew", {"on": True, "passphrase": "p"})
        self.assertFalse(r["ok"])  # no lease yet
        st = self.be.st
        st.data["lease"] = {"seller": SESH_A, "auto_renew": False, "current": 1, "contract": "0x0", "chainid": 42161}
        st.save()
        with mock.patch.object(ui.subprocess, "Popen") as P:
            P.return_value.poll.return_value = None
            code, r = self.post("/api/autorenew", {"on": True, "passphrase": "p"})
            self.assertTrue(r["ok"], r)
            cmd = P.call_args[0][0]
            self.assertEqual(cmd[-2:], ["renew", "--loop"])
            self.assertTrue(json.loads((self.home / "state.json").read_text())["lease"]["auto_renew"])
            code, r = self.post("/api/autorenew", {"on": False})
            self.assertEqual(self.be.calls[-1][0], ["stop"])
            P.return_value.terminate.assert_called()

    def test_status_without_passphrase(self):
        code, r = self.post("/api/status", {})
        self.assertTrue(r["ok"])
        self.assertIsNone(r["address"])
        self.assertFalse(r.get("unlocked"))
        self.assertNotIn("deposited", r)

    def test_unknown_path(self):
        code, _ = self.post("/api/nope", {})
        self.assertEqual(code, 404)


class RunTest(unittest.TestCase):
    def test_opens_browser_with_xdg_open(self):
        home = Path(tempfile.mkdtemp())
        with mock.patch.object(ui.shutil, "which", return_value="/usr/bin/xdg-open"), \
             mock.patch.object(ui.subprocess, "Popen") as P, \
             mock.patch.object(ui.ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(ui.run(state.State(home)), 0)
        cmd = P.call_args[0][0]
        self.assertTrue(cmd[0].endswith("xdg-open"))
        self.assertTrue(cmd[1].startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()


class RetiredPageTest(unittest.TestCase):
    def test_page_retired_api_kept(self):
        be = ui.Backend(state.State(Path(tempfile.mkdtemp())))
        srv, url = ui.serve(be)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(url)
            self.assertEqual(cm.exception.code, 404)
        finally:
            srv.shutdown(); srv.server_close()
