"""HTTP front end shared by the primary host and mirrors.  JSON only, never HTML.

  GET  /v1/info /v1/delegation /v1/checkpoint /v1/policy
  GET  /v1/log?since=N&limit=M   /v1/records?since=N&limit=M   /v1/post/<id>   /v1/thread/<id>
  POST /v1/submit   (post envelope)          primary: accept; mirror: forward to primary
  POST /v1/delete   (author delete record)   primary: apply; mirror: forward
  POST /v1/publish  (primary -> mirror push) mirror only
"""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MAX_POST = 32 * 1024
MAX_PUBLISH = 8 << 20


def _get(node, path, q):
    parts = path.strip("/").split("/")
    st = node.store
    since = int(q.get("since", ["0"])[0])
    limit = max(1, min(500, int(q.get("limit", ["200"])[0])))
    simple = {"/v1/info": node.info, "/v1/delegation": lambda: node.delegation,
              "/v1/checkpoint": node.checkpoint, "/v1/policy": lambda: node.policy}
    if path in simple:
        v = simple[path]()
        return (200, v) if v else (503, {"error": "not ready"})
    if path == "/v1/log":
        return 200, {"entries": st.since(since, limit)}
    if path == "/v1/records":
        return 200, {"records": st.records_since(since, limit)}
    if len(parts) == 3 and parts[:2] == ["v1", "post"]:
        e = st.get(parts[2])
        return (200, e) if e else (404, {"error": "not found"})
    if len(parts) == 3 and parts[:2] == ["v1", "thread"]:
        return 200, {"root": st.get(parts[2]), "replies": st.replies(parts[2])}
    return 404, {"error": "not found"}


def make_handler(resolve):
    """resolve(path) -> (node, subpath) or (None, None).  A plain node is wrapped so every
    path maps to it; a Hub maps /c/<collection>/... to that collection's node."""
    if not callable(resolve):
        node0 = resolve
        resolve = lambda p: (node0, p)  # noqa: E731

    class H(BaseHTTPRequestHandler):
        server_version = "srnet-typed"
        sys_version = ""

        def address_string(self):  # never log client addresses
            return "-"

        def log_message(self, *a):
            pass

        def _send(self, code: int, obj):
            b = json.dumps(obj, separators=(",", ":")).encode()
            self._raw(code, b, "application/json; charset=utf-8")

        def _raw(self, code, b, ctype, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.send_header("Content-Length", str(len(b)))
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/v1/collections" and hasattr(resolve, "collections"):
                return self._send(200, resolve.collections())
            node, sub = resolve(u.path)
            if node is None:
                return self._send(404, {"error": "not found"})
            if sub.startswith("/v1/upload/") and getattr(node, "blobs", None):
                return self._send(*node.blobs.status(sub.rsplit("/", 1)[1]))
            if sub.startswith("/v1/blob/") and getattr(node, "blobs", None):
                b = node.blobs.get(sub.rsplit("/", 1)[1])
                if b is None:
                    return self._send(404, {"error": "no such blob"})
                return self._raw(200, b, "application/octet-stream",
                                 [("Content-Disposition", "attachment")])
            try:
                self._send(*_get(node, sub, parse_qs(u.query)))
            except ValueError:
                self._send(400, {"error": "bad query"})

        def do_POST(self):
            node, sub = resolve(urlparse(self.path).path)
            if node is None:
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if sub == "/v1/blob" and getattr(node, "blobs", None):
                if n <= 0 or n > node.blobs.max_bytes:
                    return self._send(413, {"ok": False, "error": "size"})
                return self._send(*node.blobs.put(self.rfile.read(n)))
            if sub == "/v1/upload" and getattr(node, "blobs", None):
                if n <= 0 or n > MAX_POST:
                    return self._send(413, {"ok": False, "error": "size"})
                try:
                    env = json.loads(self.rfile.read(n))
                except ValueError:
                    return self._send(400, {"ok": False, "error": "bad json"})
                return self._send(*node.blobs.start(env, node.clock()))
            if sub.startswith("/v1/upload/") and getattr(node, "blobs", None):
                from .hub import UP_CHUNK
                if n < 0 or n > UP_CHUNK:
                    return self._send(413, {"ok": False, "error": "chunk size"})
                try:
                    off = int(parse_qs(urlparse(self.path).query).get("off", ["-1"])[0])
                except ValueError:
                    return self._send(400, {"ok": False, "error": "offset"})
                return self._send(*node.blobs.chunk(sub.rsplit("/", 1)[1], off,
                                                    self.rfile.read(n)))
            routes = {"/v1/submit": (node.submit, MAX_POST), "/v1/delete": (node.delete, MAX_POST)}
            if hasattr(node, "publish"):
                routes["/v1/publish"] = (node.publish, MAX_PUBLISH)
            if sub not in routes:
                return self._send(404, {"error": "not found"})
            fn, cap = routes[sub]
            if n <= 0 or n > cap:
                return self._send(413, {"ok": False, "error": "size"})
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return self._send(400, {"ok": False, "error": "json"})
            self._send(*fn(body))
    return H


def bind_ok(addr: str) -> bool:
    """Loopback, RFC1918 or IPv6 ULA (Session Router TUN addresses) only."""
    if addr == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(addr.strip("[]"))
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.version == 4:
        return ip.is_private and not ip.is_unspecified
    return ip in ipaddress.ip_network("fc00::/7")


class _Srv4(ThreadingHTTPServer):
    daemon_threads = True


class _Srv6(ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET6


def serve(node, addr: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    if not bind_ok(addr):
        raise SystemExit(f"refusing bind to {addr}: use loopback, private or fd00::/8 ULA")
    a = addr.strip("[]")
    cls = _Srv6 if ":" in a else _Srv4
    srv = cls((a, port), make_handler(node))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def url_of(srv) -> str:
    h, p = srv.server_address[:2]
    return f"http://[{h}]:{p}" if ":" in h else f"http://{h}:{p}"
