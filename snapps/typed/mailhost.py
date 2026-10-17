"""SRNet mail host: store-and-forward of sealed mail. Loopback by default.

The host stores opaque sealed blobs per recipient (Ed25519 identity), deletes them when the
recipient acks them after saving (fetch, then ack), and purges expired ones.  It never sees content or senders.

  GET  /v1/mail/info                       host id, limits, optional delegation
  POST /v1/mail/put        {to,msg_id,blob,ttl?,tag?,stamp?}
  POST /v1/mail/challenge  {to}            -> {nonce}   (single use, 120 s)
  POST /v1/mail/auth       {to,op,nonce,args,sig}   op: register | list | fetch | ack | tags

Piles: mail whose contact tag matches one the recipient uploaded goes to "inbox"; everything
else to "requests" (smaller size limit, fewer messages, rate-limited per recipient).
`stamp` is accepted and passed to Host.stamp_check (a no-op hook for a future paid stamp).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import mail as M
from .host import load_host_key
import mbfmt as F

DEFAULTS = {
    "ttl_default_s": 7 * 86400, "ttl_max_s": 30 * 86400,
    "inbox_max_msgs": 256, "inbox_max_bytes": 8 << 20,
    "requests_max_msgs": 32, "requests_max_blob": 16 * 1024 + 64, "requests_per_hour": 20,
    "max_inboxes": 1000, "open_registration": True, "max_tags": 512,
    "total_max_bytes": 1 << 30, "purge_every_s": 60,
}
CHALLENGE_TTL = 120


def _readjson(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Host:
    def __init__(self, data_dir, policy=None, clock=time.time):
        self.dir = os.path.abspath(data_dir)
        os.makedirs(os.path.join(self.dir, "mail"), mode=0o700, exist_ok=True)
        self.key = load_host_key(self.dir)
        self.id = F.pub_hex(self.key)
        self.p = dict(DEFAULTS, **(policy or {}))
        self.clock, self.lock = clock, threading.RLock()
        self.nonces: dict[str, tuple[str, float]] = {}
        self.req_rate: dict[str, deque] = {}
        self.reg_p = os.path.join(self.dir, "inboxes.json")
        self.inboxes = _readjson(self.reg_p) if os.path.exists(self.reg_p) else {}
        dp = os.path.join(self.dir, "delegation.json")
        self.delegation = _readjson(dp) if os.path.exists(dp) else None

    # hook for a future SRNet-balance stamp; return an error string to refuse
    def stamp_check(self, stamp, to, pile):
        return None

    def _save_reg(self):
        tmp = self.reg_p + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_BINARY', 0) | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(self.inboxes, f)
        os.replace(tmp, self.reg_p)

    def _box(self, to, pile):
        return os.path.join(self.dir, "mail", to, pile)

    def _metas(self, to):
        out = []
        for pile in ("inbox", "requests"):
            d = self._box(to, pile)
            if not os.path.isdir(d):
                continue
            for n in os.listdir(d):
                if n.endswith(".meta.json"):
                    try:
                        m = _readjson(os.path.join(d, n))
                    except (OSError, ValueError):
                        continue
                    out.append(dict(m, pile=pile))
        return out

    def _total_bytes(self):
        t = 0
        for r, _, fs in os.walk(os.path.join(self.dir, "mail")):
            t += sum(os.path.getsize(os.path.join(r, f)) for f in fs if f.endswith(".bin"))
        return t

    def info(self):
        lim = {k: self.p[k] for k in ("ttl_default_s", "ttl_max_s", "inbox_max_msgs",
                                      "requests_max_msgs", "requests_max_blob", "open_registration")}
        return {"v": 1, "role": "mail", "host": self.id, "max_blob": M.MAX_SEALED,
                "limits": lim, "delegation": self.delegation}

    # ---- writes ----
    def put(self, r: dict):
        to, mid = r.get("to"), r.get("msg_id")
        if not isinstance(to, str) or not M.HEX64.match(to) or not isinstance(mid, str) or not M.MSGID.match(mid):
            return 400, {"ok": False, "error": "bad to / msg_id"}
        try:
            blob = M.b64d(r.get("blob", ""))
        except (ValueError, TypeError):
            return 400, {"ok": False, "error": "bad blob"}
        if len(blob) > M.MAX_SEALED or blob[:4] != M.MAGIC:
            return 413, {"ok": False, "error": "too_large"}
        ttl = r.get("ttl") or self.p["ttl_default_s"]
        if not isinstance(ttl, int) or ttl <= 0:
            return 400, {"ok": False, "error": "bad ttl"}
        ttl = min(ttl, self.p["ttl_max_s"])
        with self.lock:
            reg = self.inboxes.get(to)
            if reg is None:
                return 404, {"ok": False, "error": "no_such_inbox"}
            tag = r.get("tag")
            pile = "inbox" if isinstance(tag, str) and tag in reg.get("tags", []) else "requests"
            err = self.stamp_check(r.get("stamp"), to, pile)
            if err:
                return 402, {"ok": False, "error": err}
            metas = self._metas(to)
            if any(m["msg_id"] == mid for m in metas):
                return 200, {"ok": True, "dup": True}
            mine = [m for m in metas if m["pile"] == pile]
            now = self.clock()
            if pile == "requests":
                if len(blob) > self.p["requests_max_blob"]:
                    return 413, {"ok": False, "error": "too_large (requests pile)"}
                if len(mine) >= self.p["requests_max_msgs"]:
                    return 413, {"ok": False, "error": "quota"}
                q = self.req_rate.setdefault(to, deque())
                while q and q[0] < now - 3600:
                    q.popleft()
                if len(q) >= self.p["requests_per_hour"]:
                    return 429, {"ok": False, "error": "rate limited (requests pile)"}
                q.append(now)
            elif len(mine) >= self.p["inbox_max_msgs"] or \
                    sum(m["size"] for m in mine) + len(blob) > self.p["inbox_max_bytes"]:
                return 413, {"ok": False, "error": "quota"}
            if self._total_bytes() + len(blob) > self.p["total_max_bytes"]:
                return 507, {"ok": False, "error": "host full"}
            d = self._box(to, pile)
            os.makedirs(d, mode=0o700, exist_ok=True)
            with open(os.open(os.path.join(d, mid + ".bin"), os.O_WRONLY | os.O_CREAT | getattr(os, 'O_BINARY', 0) | os.O_EXCL, 0o600), "wb") as f:
                f.write(blob)
            meta = {"msg_id": mid, "size": len(blob), "ts": int(now), "exp": int(now + ttl)}
            with open(os.path.join(d, mid + ".meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)
        return 200, {"ok": True, "pile": pile}

    def challenge(self, r):
        to = r.get("to")
        if not isinstance(to, str) or not M.HEX64.match(to):
            return 400, {"ok": False, "error": "bad to"}
        n = secrets.token_hex(16)
        with self.lock:
            now = self.clock()
            self.nonces = {k: v for k, v in self.nonces.items() if v[1] > now}
            if len(self.nonces) > 10000:
                return 429, {"ok": False, "error": "busy"}
            self.nonces[n] = (to, now + CHALLENGE_TTL)
        return 200, {"ok": True, "nonce": n, "host": self.id}

    def auth(self, r):
        n = r.get("nonce")
        with self.lock:
            ent = self.nonces.pop(n, None) if isinstance(n, str) else None
        if not ent or ent[0] != r.get("to") or ent[1] < self.clock():
            return 401, {"ok": False, "error": "unknown or expired challenge"}
        if not M.verify_auth(r, self.id):
            return 401, {"ok": False, "error": "signature does not match this inbox"}
        to, op, args = r["to"], r["op"], r.get("args") or {}
        with self.lock:
            if op == "register":
                if to not in self.inboxes:
                    if not self.p["open_registration"] or len(self.inboxes) >= self.p["max_inboxes"]:
                        return 403, {"ok": False, "error": "registration closed"}
                    self.inboxes[to] = {"tags": [], "since": int(self.clock())}
                    self._save_reg()
                return 200, {"ok": True}
            if to not in self.inboxes:
                return 404, {"ok": False, "error": "no_such_inbox"}
            if op == "tags":
                tags = args.get("tags")
                if not isinstance(tags, list) or len(tags) > self.p["max_tags"] or \
                        not all(isinstance(t, str) and M.TAG_RE.match(t) for t in tags):
                    return 400, {"ok": False, "error": "bad tags"}
                self.inboxes[to]["tags"] = sorted(set(tags))
                self._save_reg()
                return 200, {"ok": True, "n": len(tags)}
            if op == "list":
                return 200, {"ok": True, "msgs": [{k: m[k] for k in ("msg_id", "size", "ts", "exp", "pile")}
                                                  for m in self._metas(to)]}
            if op == "fetch":
                ids = args.get("ids")
                if not isinstance(ids, list) or len(ids) > 64:
                    return 400, {"ok": False, "error": "bad ids"}
                out = []
                for m in self._metas(to):
                    if m["msg_id"] in ids:
                        d = self._box(to, m["pile"])
                        with open(os.path.join(d, m["msg_id"] + ".bin"), "rb") as f:
                            out.append({"msg_id": m["msg_id"], "pile": m["pile"], "blob": M.b64e(f.read())})
                return 200, {"ok": True, "msgs": out}  # kept until "ack" (or TTL)
            if op == "ack":
                ids = args.get("ids")
                if not isinstance(ids, list) or len(ids) > 64:
                    return 400, {"ok": False, "error": "bad ids"}
                n = 0
                for m in self._metas(to):
                    if m["msg_id"] in ids:
                        self._rm(self._box(to, m["pile"]), m["msg_id"])
                        n += 1
                return 200, {"ok": True, "deleted": n}
        return 400, {"ok": False, "error": "unknown op"}

    def _rm(self, d, mid):
        for s in (".bin", ".meta.json"):
            try:
                os.remove(os.path.join(d, mid + s))
            except FileNotFoundError:
                pass

    def purge(self) -> int:
        n, now = 0, self.clock()
        with self.lock:
            for to in list(self.inboxes):
                for m in self._metas(to):
                    if m["exp"] <= now:
                        self._rm(self._box(to, m["pile"]), m["msg_id"])
                        n += 1
        return n


MAX_REQ = M.MAX_SEALED * 4 // 3 + 4096


def make_handler(h: Host):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/v1/mail/info":
                return self._json(200, h.info())
            if self.path == "/health":
                return self._json(200, {"ok": True})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_REQ:
                return self._json(413, {"ok": False, "error": "too_large"})
            try:
                r = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(r, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "error": "bad json"})
            fn = {"/v1/mail/put": h.put, "/v1/mail/challenge": h.challenge,
                  "/v1/mail/auth": h.auth}.get(self.path)
            if not fn:
                return self._json(404, {"error": "not found"})
            self._json(*fn(r))
    return H


def serve(h: Host, bind="127.0.0.1", port=0, purge=True):
    srv = ThreadingHTTPServer((bind, port), make_handler(h))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if purge:
        def loop():
            while True:
                time.sleep(h.p["purge_every_s"])
                try:
                    h.purge()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(description="SRNet mail host (store-and-forward, sealed)")
    ap.add_argument("--data", required=True)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48915)
    ap.add_argument("--closed", action="store_true", help="no new inbox registrations")
    ap.add_argument("--delegation", help="root-signed delegation to publish in /v1/mail/info")
    ap.add_argument("--max-inboxes", type=int, default=DEFAULTS["max_inboxes"])
    a = ap.parse_args(argv)
    h = Host(a.data, {"open_registration": not a.closed, "max_inboxes": a.max_inboxes})
    if a.delegation and os.path.exists(a.delegation):
        h.delegation = _readjson(a.delegation)
    srv = serve(h, a.bind, a.port)
    print(f"mail host {h.id[:16]}… on http://{a.bind}:{srv.server_address[1]}/", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
