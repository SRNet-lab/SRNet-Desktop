"""Local client: verified cache + bundled template UI on 127.0.0.1.

The UI is shipped with the client (mbclient/template/); hosts only ever provide signed JSON
data.  The template renders every post with textContent, never as HTML.

Local API (JSON):  GET /api/state, /api/timeline?view=all|following, /api/thread/<id>
                   POST /api/sync, /api/post {body, reply_to}, /api/delete {id},
                   /api/follow {author, on}, /api/mute {author, on},
                   /api/moderation {mode: "honour"|"ignore"}, /api/reveal {id, on}

Moderation: hosts publish signed hide/unhide records.  By default the client honours them;
the user can ignore host moderation entirely, or reveal single hidden posts.  Mutes are local.
Outbox: a post that no host accepts because none is reachable is kept and retried on sync
(re-signed with a fresh timestamp, since hosts refuse stale ones).
"""
from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import mbfmt as F
from mbhost.store import Store
from typed.sync import SyncError, http_json, submit, sync_once

TEMPLATE = Path(__file__).resolve().parent / "template"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Client:
    def __init__(self, data_dir: str, board: str, root_pin: str, hosts: list[str]):
        os.makedirs(data_dir, exist_ok=True)
        self.dir, self.board, self.root_pin, self.hosts = data_dir, board, root_pin, list(hosts)
        kp = os.path.join(data_dir, "author.key")  # author signing key; NOT the transport key
        if not os.path.exists(kp):
            with open(os.open(kp, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
                f.write(os.urandom(32))
        self.key = F.key_from_seed(Path(kp).read_bytes())
        self.me = F.pub_hex(self.key)
        self.store = Store(os.path.join(data_dir, "cache.sqlite3"))
        self.prefs_path = os.path.join(data_dir, "prefs.json")
        self.prefs = {"follows": [], "mutes": [], "pin": [0, F.GENESIS], "moderation": "honour",
                      "revealed": [], "outbox": []}
        if os.path.exists(self.prefs_path):
            self.prefs.update(json.loads(Path(self.prefs_path).read_text()))
        self.pin = F.CheckpointPin(*self.prefs["pin"])
        self.last = {"host": None, "error": None, "added": 0}
        self.policy = self.host_pub = None
        self.lock = threading.Lock()

    def _save(self):
        self.prefs["pin"] = [self.pin.seq, self.pin.head]
        tmp = self.prefs_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.prefs, f)
        os.replace(tmp, self.prefs_path)

    def sync(self) -> dict:
        with self.lock:
            # remember the last good host and try it first
            order = sorted(self.hosts, key=lambda h: h != self.last.get("host"))
            try:
                r = sync_once(self.store, order, self.root_pin, self.board, self.pin)
                self.last = {"host": r["host"], "error": None, "added": r["added"],
                             "records": r["records"], "skipped": r["errors"]}
                self.policy = r.get("policy")
                self.host_pub = r.get("host_pub")
            except SyncError as e:
                self.last = dict(self.last, error=str(e), added=0)
            self._save()
        if self.prefs["outbox"] and not self.last.get("error"):
            self.flush_outbox()
        return self.last

    def flush_outbox(self) -> list:
        sent, keep = [], []
        for item in list(self.prefs["outbox"]):
            r = self._post_now(item["body"], item.get("reply_to"))
            (keep if r.get("unreachable") else sent).append(item if r.get("unreachable") else r)
        with self.lock:
            self.prefs["outbox"] = keep
            self._save()
        if any(x.get("ok") for x in sent):
            self.sync()
        return sent

    def _pow_bits(self) -> int:
        """New keys need more work.  Known posts by us in a verified checkpoint = not new
        (the host decides; we pick the higher figure until we have posted)."""
        pol = self.policy
        if pol is None:
            for h in self.hosts:
                try:
                    pol = http_json(h + "/v1/info")[1]
                    break
                except (OSError, SyncError, ValueError):
                    continue
        if not pol or "pow_bits" not in pol:
            return None  # no host reachable: don't burn CPU, let the caller queue
        mine = self.store.db.execute("SELECT MIN(ts) FROM log WHERE author=?",
                                     (self.me,)).fetchone()[0]
        age = pol.get("new_key_age_s", 86400)
        import time as _t
        new = mine is None or _t.time() - mine < age
        return int(pol["new_key_pow_bits"] if new else pol["pow_bits"])

    def _post_now(self, body, reply_to):
        bits = self._pow_bits()
        if bits is None:
            return {"ok": False, "unreachable": True, "error": "no host reachable"}
        try:
            env = F.make_post(self.key, self.board, body, reply_to or None, pow_bits=bits)
        except F.FormatError as e:
            return {"ok": False, "error": str(e)}
        return submit(self.hosts, env)

    def post(self, body: str, reply_to: str | None = None) -> dict:
        try:  # validate now so a bad post never sits in the outbox
            F.make_post(self.key, self.board, body, reply_to or None)
        except F.FormatError as e:
            return {"ok": False, "error": str(e)}
        r = self._post_now(body, reply_to)
        if r.get("ok"):
            self.sync()
        elif r.get("unreachable"):
            with self.lock:
                self.prefs["outbox"].append({"body": body, "reply_to": reply_to})
                self._save()
            r = dict(r, queued=True, error="no host reachable; kept in outbox")
        return r

    def delete(self, pid: str) -> dict:
        e = self.store.get(pid)
        if not e or "env" not in e or e["env"]["post"]["author"] != self.me:
            return {"ok": False, "error": "not your post"}
        r = submit(self.hosts, F.make_delete(self.key, self.board, [pid]), "/v1/delete")
        if r.get("ok"):
            self.sync()
        return r

    # ---- moderation (host-signed records, honoured unless overridden locally) ----
    def hidden(self) -> tuple[set, set]:
        posts, authors = {}, {}
        for r in self.store.records_since(0, 1 << 30):
            if r["kind"] != "mod":
                continue
            m = r["rec"]["mod"]
            (posts if m["target_type"] == "post" else authors)[m["target"]] = m["action"] == "hide"
        return ({k for k, v in posts.items() if v}, {k for k, v in authors.items() if v})

    def _visible(self, e, hp, ha):
        if "env" not in e:
            return False
        a = e["env"]["post"]["author"]
        if a in self.prefs["mutes"]:
            return False
        if self.prefs["moderation"] == "ignore" or e["id"] in self.prefs["revealed"]:
            return True
        return e["id"] not in hp and a not in ha

    def _view(self, e, hidden=False):
        if "env" not in e:
            return {"id": e["id"], "seq": e["seq"], "gone": e["tomb"].get("by")}
        p = e["env"]["post"]
        return {"id": e["id"], "seq": e["seq"], "author": p["author"], "ts": p["ts"],
                "body": p["body"], "reply_to": p["reply_to"], "mine": p["author"] == self.me,
                "followed": p["author"] in self.prefs["follows"], "host_hidden": hidden}

    def timeline(self, view="all", limit=200):
        follows = set(self.prefs["follows"]) | {self.me}
        hp, ha = self.hidden()
        out, nhidden = [], 0
        for e in reversed(self.store.since(0, 100000)):
            if "env" not in e:
                continue
            a = e["env"]["post"]["author"]
            if view == "following" and a not in follows:
                continue
            if not self._visible(e, hp, ha):
                nhidden += a not in self.prefs["mutes"]
                continue
            v = self._view(e, e["id"] in hp or a in ha)
            v["replies"] = sum(1 for r in self.store.replies(e["id"]) if self._visible(r, hp, ha))
            out.append(v)
            if len(out) >= limit:
                break
        self.last_hidden = nhidden
        return out

    def thread(self, pid):
        hp, ha = self.hidden()
        root = self.store.get(pid)
        chain = []
        cur = root
        while cur and cur["env"]["post"]["reply_to"] and len(chain) < 50:
            cur = self.store.get(cur["env"]["post"]["reply_to"])
            if cur and "env" not in cur:
                chain.insert(0, self._view(cur))
                break
            if cur and self._visible(cur, hp, ha):
                chain.insert(0, self._view(cur))
        rv = self._view(root) if root and (self._visible(root, hp, ha) or "env" not in root) \
            else None
        return {"root": rv, "parents": chain,
                "replies": [self._view(r) for r in self.store.replies(pid)
                            if self._visible(r, hp, ha)]}

    def toggle(self, kind, author, on):
        F.load_pub(author)
        s = set(self.prefs[kind])
        (s.add if on else s.discard)(author)
        self.prefs[kind] = sorted(s)
        self._save()
        return {"ok": True, kind: self.prefs[kind]}

    def set_moderation(self, mode):
        if mode not in ("honour", "ignore"):
            raise ValueError("mode")
        self.prefs["moderation"] = mode
        self._save()
        return {"ok": True, "moderation": mode}

    def reveal(self, pid, on=True):
        s = set(self.prefs["revealed"])
        (s.add if on else s.discard)(pid)
        self.prefs["revealed"] = sorted(s)
        self._save()
        return {"ok": True}

    def state(self):
        return {"me": self.me, "board": self.board, "hosts": self.hosts, "seq": self.pin.seq,
                "follows": self.prefs["follows"], "mutes": self.prefs["mutes"], "last": self.last,
                "moderation": self.prefs["moderation"], "outbox": len(self.prefs["outbox"]),
                "hidden": getattr(self, "last_hidden", 0),
                "rules": (self.policy or {}).get("rules", "")}


def make_handler(c: Client):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _hdr(self, code, ctype, n):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(n))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self._hdr(code, "application/json; charset=utf-8", len(b))
            self.wfile.write(b)

        def _local_only(self):
            # refuse cross-origin / DNS-rebinding writes to the local API
            hosthdr = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            origin = self.headers.get("Origin")
            okhost = hosthdr in ("127.0.0.1", "localhost", "[::1]")
            return okhost and (origin is None or urlparse(origin).hostname in
                               ("127.0.0.1", "localhost", "::1"))

        def do_GET(self):
            u = urlparse(self.path)
            if not self._local_only():
                return self._json(403, {"error": "local only"})
            if u.path in STATIC:
                name, ct = STATIC[u.path]
                b = (TEMPLATE / name).read_bytes()
                self._hdr(200, ct, len(b))
                return self.wfile.write(b)
            if u.path == "/api/state":
                return self._json(200, c.state())
            if u.path == "/api/timeline":
                v = parse_qs(u.query).get("view", ["all"])[0]
                return self._json(200, {"posts": c.timeline(v)})
            if u.path.startswith("/api/thread/"):
                return self._json(200, c.thread(u.path.rsplit("/", 1)[1]))
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_only() or not (self.headers.get("Content-Type") or "").startswith(
                    "application/json"):
                return self._json(403, {"error": "local JSON only"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                a = json.loads(self.rfile.read(min(n, 8192)) or b"{}")
                if self.path == "/api/sync":
                    return self._json(200, c.sync())
                if self.path == "/api/post":
                    return self._json(200, c.post(str(a.get("body", "")), a.get("reply_to")))
                if self.path == "/api/delete":
                    return self._json(200, c.delete(str(a["id"])))
                if self.path == "/api/moderation":
                    return self._json(200, c.set_moderation(a.get("mode")))
                if self.path == "/api/reveal":
                    return self._json(200, c.reveal(str(a["id"]), bool(a.get("on", True))))
                if self.path in ("/api/follow", "/api/mute"):
                    kind = "follows" if self.path == "/api/follow" else "mutes"
                    return self._json(200, c.toggle(kind, a["author"], bool(a.get("on", True))))
            except (ValueError, KeyError, F.FormatError) as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})
    return H


def serve(c: Client, port=0, sync_every=5.0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(c))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if sync_every:
        def loop():
            ev = threading.Event()
            while not ev.wait(sync_every):
                c.sync()
        threading.Thread(target=loop, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser(description="srnet-msgboard client (phase 0)")
    ap.add_argument("--data", required=True)
    ap.add_argument("--board", default="demo")
    ap.add_argument("--root-pin", required=True)
    ap.add_argument("--host", action="append", required=True, help="repeat for failover order")
    ap.add_argument("--port", type=int, default=48900)
    a = ap.parse_args()
    c = Client(a.data, a.board, a.root_pin, a.host)
    c.sync()
    srv = serve(c, a.port)
    print(f"client UI: http://127.0.0.1:{srv.server_address[1]}/  (author {c.me[:16]}…)",
          flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
