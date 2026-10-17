import json, re, unittest, urllib.request
from pathlib import Path
from helpers import F, start_host, tmpdir
from mbclient.client import Client, serve, CSP

TPL = Path(__file__).resolve().parent.parent / "mbclient/template"
PAYLOAD = '<img src=x onerror="alert(1)"><script>alert(2)</script>'


class XSS(unittest.TestCase):
    def test_template_has_no_html_sinks(self):
        js = (TPL / "app.js").read_text()
        code = "\n".join(l for l in js.splitlines() if not l.strip().startswith("//")
                             and not l.strip().startswith('frame.src = "/frame/" + encodeURIComponent(')
                             and not l.strip().startswith('$("w-qr").src = URL.createObjectURL(await q.blob());'))
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                     "eval(", "new Function", ".src =", ".href =", "setAttribute("):
            self.assertNotIn(sink, code, sink)
        self.assertIn("textContent", code)
        html = (TPL / "index.html").read_text()
        self.assertNotRegex(html, r"<script>(?!</script>)")  # no inline script
        self.assertNotRegex(html, r"\son\w+=")               # no inline handlers

    def test_payload_round_trips_as_text_with_strict_csp(self):
        h, srv, url, root = start_host(tmpdir())
        c = Client(tmpdir(), "demo", root, [url])
        csrv = serve(c, sync_every=0)
        base = f"http://127.0.0.1:{csrv.server_address[1]}"
        try:
            self.assertTrue(c.post(PAYLOAD)["ok"])
            r = urllib.request.urlopen(base + "/api/timeline")
            self.assertTrue(r.headers["Content-Type"].startswith("application/json"))
            self.assertEqual(r.headers["Content-Security-Policy"], CSP)
            self.assertNotIn("unsafe-inline", CSP)
            posts = json.loads(r.read())["posts"]
            self.assertEqual(posts[0]["body"], PAYLOAD)  # data, verbatim, for textContent
            page = urllib.request.urlopen(base + "/")
            self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
            self.assertNotIn("alert", page.read().decode())  # post data never in served HTML
        finally:
            csrv.shutdown(); srv.shutdown()

    def test_local_api_refuses_foreign_origin(self):
        h, srv, url, root = start_host(tmpdir())
        c = Client(tmpdir(), "demo", root, [url])
        csrv = serve(c, sync_every=0)
        base = f"http://127.0.0.1:{csrv.server_address[1]}"
        try:
            req = urllib.request.Request(base + "/api/post", data=b'{"body":"csrf"}',
                                         headers={"Content-Type": "application/json",
                                                  "Origin": "http://evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 403)
        finally:
            csrv.shutdown(); srv.shutdown()


if __name__ == "__main__":
    unittest.main()


class TypedTemplates(unittest.TestCase):
    def test_no_html_sinks_in_any_template_or_shell(self):
        root = Path(__file__).resolve().parent.parent / "typed"
        files = list((root / "templates").glob("*/*.js")) + [root / "shell" / "shell.js"]
        self.assertGreater(len(files), 8)
        for f in files:
            code = "\n".join(l for l in f.read_text().splitlines() if not l.strip().startswith("//")
                             and not l.strip().startswith('frame.src = "/frame/" + encodeURIComponent(')
                             and not l.strip().startswith('$("w-qr").src = URL.createObjectURL(await q.blob());'))
            for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                         "eval(", "new Function", "setAttribute(", ".src =", "srcdoc"):
                self.assertNotIn(sink, code, f"{f}: {sink}")
