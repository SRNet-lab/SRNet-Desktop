"""Add-on tabs (e.g. Exits from srnet-exits): the core lists add-ons from SRNET_ADDONS, serves each
tab script from its own origin and forwards X-Core-gated calls to the add-on backend with its token.
Without add-ons the app has only Home. The browser part needs playwright + Chrome (skipped otherwise)."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path[:0] = [str(HERE.parents[1]), str(HERE.parents[2] / "client-cli")]
from test_room_ui import CHROME, sync_playwright  # noqa: E402
from typed import testbed as T  # noqa: E402
from typed.core import Core, addons_from_env, serve as core_serve  # noqa: E402

TOK = "demo-token-0123456789"
TAB_JS = b"""(() => { window.SRNetAddons = window.SRNetAddons || {};
  let sh = null, root = null;
  window.SRNetAddons.demo = {
    mount(sec, shell) { sh = shell; root = sec; const h = shell.el("h2", "Demo tab"); const b = shell.el("button", "Ping");
      const out = shell.el("p", ""); out.id = "demo-out";
      b.addEventListener("click", async () => { const r = await sh.api("addon/demo/ping", {x: 1}); out.textContent = "pong " + r.pong; });
      sec.append(h, b, out); },
    show() { root.dataset.shown = "1"; } }; })();
"""


class FakeAddon:
    def __init__(self):
        self.calls = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ct="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self.path.endswith("k=" + TOK):
                    return self._send(403, b"no")
                if self.path.startswith("/tab.js"):
                    return self._send(200, TAB_JS, "text/javascript")
                if self.path.startswith("/tab.css"):
                    return self._send(200, b"#demo-out{color:#3b6}", "text/css")
                if self.path.startswith("/qr.svg?q=0123456789abcdef"):
                    return self._send(200, b"<svg></svg>", "image/svg+xml")
                return self._send(404, b"{}")

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                me.calls.append((self.path, self.headers.get("X-CSRF-Token"), body))
                if self.headers.get("X-CSRF-Token") != TOK:
                    return self._send(403, b'{"ok": false}')
                return self._send(200, json.dumps({"ok": True, "pong": 1}).encode())
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/#{TOK}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def req(base, path, body=None, core=True):
    h = {"X-Core": "1"} if core else {}
    data = None
    if body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=h), timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Bridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addon = FakeAddon()
        cls.env = T.start(mirror=False)
        env = json.dumps([{"name": "demo", "title": "Demo", "url": cls.addon.url},
                          {"name": "evil", "title": "x", "url": "http://example.com:80/#" + "a" * 20},
                          {"name": "Bad Name", "title": "x", "url": cls.addon.url}])
        with mock.patch.dict(os.environ, {"SRNET_ADDONS": env}):
            cls.core = Core(tempfile.mkdtemp(prefix="addon-"), T.client_config(cls.env))
        cls.srv = core_serve(cls.core, 0, sync_every=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.addon.close()
        T.stop(cls.env)

    def test_env_parsing(self):
        self.assertEqual(sorted(self.core.addons), ["demo"])  # non-loopback and bad names dropped
        with mock.patch.dict(os.environ, {"SRNET_ADDONS": "not json"}):
            self.assertEqual(addons_from_env(), {})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SRNET_ADDONS", None)
            self.assertEqual(addons_from_env(), {})

    def test_collections_list_addons(self):
        code, body = req(self.base, "/api/collections")
        self.assertEqual(code, 200)
        out = json.loads(body)
        self.assertEqual(out["addons"], [{"name": "demo", "title": "Demo"}])
        self.assertNotIn("wallet", out)

    def test_tab_script_from_core_origin(self):
        code, body = req(self.base, "/addon/demo/tab.js", core=False)
        self.assertEqual((code, body), (200, TAB_JS))
        self.assertEqual(req(self.base, "/addon/nope/tab.js", core=False)[0], 404)
        self.assertEqual(req(self.base, "/addon/demo/other.js", core=False)[0], 403)

    def test_calls_need_x_core_and_carry_the_token(self):
        self.assertEqual(req(self.base, "/api/addon/demo/ping", {"x": 1}, core=False)[0], 403)
        code, body = req(self.base, "/api/addon/demo/ping", {"x": 1})
        self.assertEqual(json.loads(body), {"ok": True, "pong": 1})
        self.assertEqual(self.addon.calls[-1], ("/api/ping", TOK, {"x": 1}))
        out = json.loads(req(self.base, "/api/addon/nope/ping", {})[1])
        self.assertFalse(out["ok"])
        self.assertIn("isn't running", out["err"])

    def test_qr_proxy(self):
        code, body = req(self.base, "/api/addon/demo/qr.svg?q=0123456789abcdef")
        self.assertEqual((code, body), (200, b"<svg></svg>"))
        self.assertEqual(req(self.base, "/api/addon/demo/qr.svg?q=../../x")[0], 400)

    def test_preflight_is_served_by_core(self):
        fake = {"preflight": "OK", "when": "t", "host": "h", "checks": []}
        with mock.patch("srnet.preflight.run", return_value=fake), \
                mock.patch("srnet.preflight.write_report", return_value=Path("/tmp/r.txt")):
            out = json.loads(req(self.base, "/api/preflight", {})[1])
        self.assertTrue(out["ok"])
        self.assertEqual(out["report"]["preflight"], "OK")


@unittest.skipUnless(sync_playwright and CHROME, "playwright or Chrome not installed")
class Tabs(unittest.TestCase):
    def tearDown(self):
        T.stop(self._env)

    def test_no_addons_only_home(self):
        with sync_playwright() as p:
            env = T.start(mirror=False)
            self._env = env
            os.environ.pop("SRNET_ADDONS", None)
            core = Core(tempfile.mkdtemp(prefix="tabs-"), T.client_config(env))
            srv = core_serve(core, 0, sync_every=0)
            try:
                b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
                pg = b.new_page()
                pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
                pg.wait_for_selector("#home:not([hidden])", timeout=20000)
                self.assertEqual(pg.inner_text("#ribbon").strip(), "Home")
                self.assertNotIn("Wallet", pg.inner_text("body"))
                b.close()
            finally:
                srv.shutdown()
                srv.server_close()

    def test_addon_tab_mounts_and_calls_through(self):
        addon = FakeAddon()
        env = T.start(mirror=False)
        self._env = env
        try:
            with mock.patch.dict(os.environ, {"SRNET_ADDONS": json.dumps(
                    [{"name": "demo", "title": "Demo", "url": addon.url}])}):
                core = Core(tempfile.mkdtemp(prefix="tabs-"), T.client_config(env))
            srv = core_serve(core, 0, sync_every=0)
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
                pg = b.new_page()
                errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                pg.goto(f"http://127.0.0.1:{srv.server_address[1]}/?tab=demo")
                pg.wait_for_selector("section.addon[data-addon=demo]:not([hidden]) h2", timeout=20000)
                self.assertIn("Demo", pg.inner_text("#ribbon"))
                self.assertEqual(pg.get_attribute("section.addon[data-addon=demo]", "data-shown"), "1")
                pg.click("section.addon[data-addon=demo] button:has-text('Ping')")
                pg.wait_for_selector("#demo-out:has-text('pong 1')", timeout=10000)
                pg.click("#ribbon button[data-b=home]")
                self.assertTrue(pg.is_hidden("section.addon[data-addon=demo]"))
                self.assertEqual(errs, [])
                b.close()
            srv.shutdown()
            srv.server_close()
        finally:
            addon.close()


if __name__ == "__main__":
    unittest.main()
