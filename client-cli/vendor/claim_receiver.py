#!/usr/bin/env python3
"""Claim receiver: buyers deliver their signed lease claim over Session Router.

The buyer's client POSTs the claim to http://<this exit's .sesh>:<claim_port>/v1/claim
(claim_port comes from the signed listing; default 8650). The receiver checks the claim's
shape, writes it atomically into the watcher's spool and wakes the watcher, which does the
real verification (lease active, paid to our wallet, rate, buyer signature) and opens the exit.

Protocol
  POST /v1/claim         body = claim JSON {"v":1,"hold_id":N,"client_sesh":"...","sig":"0x..."}
    202 {"ok":true,"status":"queued","hold_id":N}         accepted for verification
    200 {"ok":true|false,"status":"admitted"|"rejected","reason":...}   already decided
    409 {"ok":false,"status":"conflict"}  a different claim for the same lease is already held
    400 / 413 / 429 on bad input, oversize or too many requests
  GET  /v1/claim/<hold_id>  -> current status for that lease (queued / admitted / rejected / unknown)

Idempotent: re-sending the identical claim returns the same status, so clients can retry freely.
First valid-shaped claim for a lease wins; a different claim body for that lease gets 409 (the
watcher would refuse a second claim anyway).
The receiver never moves funds and holds no keys.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 2048
SESH_RE = re.compile(r"^[ybndrfg8ejkmcpqxot1uwisza345h769]{52}\.sesh$")
SIG_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
DEFAULT_PORT = 8650


def check_shape(c) -> str | None:
    if not isinstance(c, dict) or set(c) != {"v", "hold_id", "client_sesh", "sig"}:
        return "bad_fields"
    if c["v"] != 1:
        return "bad_version"
    if not isinstance(c["hold_id"], int) or isinstance(c["hold_id"], bool) or not 0 < c["hold_id"] < 2**63:
        return "bad_hold_id"
    if not isinstance(c["client_sesh"], str) or not SESH_RE.match(c["client_sesh"]):
        return "bad_client"
    if not isinstance(c["sig"], str) or not SIG_RE.match(c["sig"]):
        return "bad_sig"
    return None


class Limiter:
    def __init__(self, per_min: int = 30, clock=time.monotonic):
        self.per_min, self.clock, self.hits, self.lock = per_min, clock, {}, threading.Lock()

    def allow(self, key: str) -> bool:
        now = self.clock()
        with self.lock:
            q = [t for t in self.hits.get(key, []) if now - t < 60]
            if len(q) >= self.per_min:
                self.hits[key] = q
                return False
            q.append(now)
            self.hits[key] = q
            return True


class Receiver:
    def __init__(self, spool: Path, wake: threading.Event | None = None, limiter: Limiter | None = None):
        self.spool = Path(spool)
        self.spool.mkdir(parents=True, exist_ok=True)
        self.wake = wake or threading.Event()
        self.limiter = limiter or Limiter()
        self.lock = threading.Lock()

    def _name(self, hold_id: int) -> str:
        return f"hold-{hold_id}.json"

    def status(self, hold_id: int) -> tuple[str, str | None, bytes | None]:
        n = self._name(hold_id)
        for d, st in ((self.spool / "done", "admitted"), (self.spool / "rejected", "rejected")):
            if (d / n).exists():
                r = d / (n + ".result")
                return st, (r.read_text().strip() if r.exists() else None), (d / n).read_bytes()
        if (self.spool / n).exists():
            return "queued", None, (self.spool / n).read_bytes()
        return "unknown", None, None

    def submit(self, claim: dict) -> tuple[int, dict]:
        err = check_shape(claim)
        if err:
            return 400, {"ok": False, "status": "invalid", "reason": err}
        hid = claim["hold_id"]
        body = json.dumps(claim, sort_keys=True, separators=(",", ":")).encode()
        with self.lock:
            st, reason, held = self.status(hid)
            if held is not None:
                try:
                    same = json.loads(held) == claim
                except ValueError:
                    same = False
                if not same:
                    return 409, {"ok": False, "status": "conflict", "hold_id": hid,
                                 "reason": "another claim for this lease is already held"}
                if st == "queued":
                    self.wake.set()
                    return 202, {"ok": True, "status": "queued", "hold_id": hid}
                return 200, {"ok": st == "admitted", "status": st, "reason": reason, "hold_id": hid}
            tmp = self.spool / f".{self._name(hid)}.{os.getpid()}.tmp"
            tmp.write_bytes(body)
            os.replace(tmp, self.spool / self._name(hid))
        self.wake.set()
        return 202, {"ok": True, "status": "queued", "hold_id": hid}


def make_handler(rx: Receiver):
    class H(BaseHTTPRequestHandler):
        server_version = "srnet-claims"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _out(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            m = re.fullmatch(r"/v1/claim/([0-9]{1,19})", self.path)
            if not m:
                return self._out(404, {"ok": False})
            st, reason, _ = rx.status(int(m.group(1)))
            return self._out(200, {"ok": st == "admitted", "status": st, "reason": reason,
                                   "hold_id": int(m.group(1))})

        def do_POST(self):
            if self.path != "/v1/claim":
                return self._out(404, {"ok": False})
            if not rx.limiter.allow(self.client_address[0]):
                return self._out(429, {"ok": False, "status": "slow_down"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._out(413, {"ok": False, "status": "too_large"})
            try:
                claim = json.loads(self.rfile.read(n))
            except ValueError:
                return self._out(400, {"ok": False, "status": "invalid", "reason": "bad_json"})
            return self._out(*rx.submit(claim))
    return H


def serve(rx: Receiver, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    cls = ThreadingHTTPServer
    if ":" in host:
        import socket

        class V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        cls = V6
    srv = cls((host, port), make_handler(rx))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
