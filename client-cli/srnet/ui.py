"""`srnet ui`: a local web page for the buyer client.

Binds 127.0.0.1 on a random port and opens the default browser. Every request needs the
per-run token from the launch URL; API calls also need it in the X-CSRF-Token header, a Host
of 127.0.0.1:<port>, and (when sent) a matching Origin. The keystore passphrase is typed into
the page, used for that one request, and never written anywhere.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import board as boardmod, chain, keys, state

WEB = Path(__file__).resolve().parent / "web"
MAX_BODY = 8 * 1024


class Backend:
    """What the page can do. Each method returns a JSON-able dict."""

    def __init__(self, st: state.State, rpc_url: str | None = None):
        self.st = st
        self.rpc_url = rpc_url
        self.renewer: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def cfg(self) -> dict:
        p = self.st.root / "config.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _rpc(self) -> str:
        return self.rpc_url or self.cfg().get("rpc") or os.environ.get("SRNET_RPC") or chain.DEFAULT_RPC

    def _keystore(self) -> Path:
        return Path(self.cfg().get("keystore") or keys.default_path(self.st.root))

    def _reload(self) -> dict:
        self.st.data = state.State(self.st.root).data
        return self.st.data

    def board(self) -> dict:
        cfg = self.cfg()
        good, info = boardmod.fetch(boardmod.sources(cfg), boardmod.pin_of(cfg), self.st.root,
                                    dns=cfg.get("sesh_dns"))
        now = time.time()
        hidden = sum(1 for d in good if not boardmod.buyable(d))
        good = [d for d in good if boardmod.buyable(d)]
        rows = [{"seller": d["listing"]["seller"], "region": d["listing"].get("region") or "",
                 "price": str(d["listing"].get("price_per_hour_usdc", d["listing"].get("price_usdc", ""))),
                 "age_s": max(0, int(now - int(d["listing"].get("issued", 0))))} for d in good]
        rows.sort(key=lambda r: r["seller"])
        return {"listings": rows, "seq": info["seq"], "source": info["source"],
                "dropped": len(info["dropped"]), "hidden": hidden}

    def status(self, passphrase: str | None) -> dict:
        from .cli import _own_sesh
        d = self._reload()
        lease = d.get("lease") or {}
        out = {"address": keys.address_of(self._keystore()), "connected": (d.get("connected") or {}).get("seller"),
               "seller": lease.get("seller"), "auto_renew": bool(lease.get("auto_renew")),
               "renewer_running": bool(self.renewer and self.renewer.poll() is None),
               "client_sesh": self.cfg().get("client_sesh") or lease.get("client_sesh") or _own_sesh(),
               "relayer": self.relayer_url(),
               "unlocked": False}
        if passphrase:
            pk = keys.unlock(self._keystore(), passphrase)
            ch = chain.ChainCfg(rpc=self._rpc(), contract=self.cfg().get("contract") or chain.DEFAULT_CONTRACT,
                                private_key=pk, relayer=self.relayer_url())
            ch.chainid()
            out["usdc_wallet"] = ch.usdc_balance() / 1e6
            out["deposited"] = ch.available() / 1e6
            out["unlocked"] = True
            if lease.get("current"):
                ch.contract = lease["contract"]
                cur = chain.newest_lease(ch, int(lease["current"]))
                _, end, closed, stopped = ch.lease_window(cur)
                out["time_left_s"] = max(0, end - ch.now())
                out["seller_stopped"] = stopped
        return out

    def _srnet(self, args: list[str], passphrase: str | None, timeout: float = 600) -> dict:
        env = dict(os.environ)
        env.pop(keys.PASSWORD_ENV, None)
        if passphrase:
            env[keys.PASSWORD_ENV] = passphrase
        cmd = [sys.executable, "-m", "srnet", "--home", str(self.st.root), *args]
        if self.rpc_url and args[0] in ("buy", "connect", "renew"):
            cmd += ["--rpc", self.rpc_url]
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parents[1]))
        return {"ok": p.returncode == 0, "out": p.stdout[-2000:], "err": p.stderr[-2000:]}

    def buy(self, seller: str, client_sesh: str, fund: bool, passphrase: str) -> dict:
        args = ["buy", seller, "--client-sesh", client_sesh, "--hours", "1"]
        if fund:
            args.append("--fund")
        return self._srnet(args, passphrase)

    def auto_renew(self, on: bool, passphrase: str | None) -> dict:
        with self.lock:
            d = self._reload()
            lease = d.get("lease")
            if not lease:
                return {"ok": False, "err": "no lease yet; buy an hour first"}
            if not on:
                if self.renewer and self.renewer.poll() is None:
                    self.renewer.terminate()
                self.renewer = None
                return self._srnet(["stop"], None)
            lease["auto_renew"] = True
            self.st.data["lease"] = lease
            self.st.save()
            if not (self.renewer and self.renewer.poll() is None):
                env = dict(os.environ)
                env[keys.PASSWORD_ENV] = passphrase or ""
                cmd = [sys.executable, "-m", "srnet", "--home", str(self.st.root), "renew", "--loop"]
                if self.rpc_url:
                    cmd += ["--rpc", self.rpc_url]
                self.renewer = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                cwd=str(Path(__file__).resolve().parents[1]))
            return {"ok": True, "auto_renew": True}

    def connect(self, seller: str, client_sesh: str, passphrase: str) -> dict:
        return self._srnet(["connect", seller, "--client-sesh", client_sesh], passphrase)

    def disconnect(self) -> dict:
        return self._srnet(["disconnect"], None)

    # ---- Wallet / Top up (phone wallet -> own SRNet wallet -> gasless deposit via relayer) ----
    def relayer_url(self) -> str:
        return chain.relayer_url(self.cfg())

    def set_relayer(self, url: str) -> dict:
        url = (url or "").strip()
        if url.lower() in chain.RELAYER_OFF:
            url = "off"
        elif url:
            u = urllib.parse.urlparse(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                return {"ok": False, "err": "relayer must be an http(s) URL"}
            if u.scheme == "http" and not (u.hostname in ("127.0.0.1", "::1", "localhost")
                                           or u.hostname.endswith(".sesh")):
                return {"ok": False, "err": "plain http is only allowed for loopback or .sesh relayers"}
        p = self.st.root / "config.json"
        cfg = self.cfg()
        if url:
            cfg["relayer"] = url
        else:
            cfg.pop("relayer", None)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2))
        return {"ok": True, "relayer": self.relayer_url()}

    def topup_info(self, amount: float | None) -> dict:
        from . import gasless
        addr = keys.address_of(self._keystore())
        if not addr:
            return {"ok": False, "err": "no wallet yet: run srnet key new"}
        cid, usdc = chain.DEFAULT_CHAINID, gasless.USDC_BY_CHAIN[chain.DEFAULT_CHAINID]
        if self._rpc() != chain.DEFAULT_RPC or self.cfg().get("contract"):  # testnet / custom: ask the chain
            ch = chain.read_only(self._rpc(), self.cfg().get("contract") or chain.DEFAULT_CONTRACT)
            cid, usdc = ch.chainid(), ch.usdc()
        base = int(round(amount * chain.USDC)) if amount else None
        uri = gasless.eip681_uri(usdc, addr, cid, base)
        warn = gasless.TOPUP_WARNING.format(address=addr, chain_name=chain.CHAINS.get(cid, cid),
                                            relay_fee=f"{gasless.DEFAULT_RELAY_FEE / chain.USDC:.2f}")
        self._qr_uri = uri
        return {"address": addr, "uri": uri, "chainid": cid, "warning": warn,
                "relay_fee": gasless.DEFAULT_RELAY_FEE / chain.USDC, "fee_pct": 3,
                "relayer": self.relayer_url(), "qr": _have_qr()}

    def qr_svg(self) -> bytes:
        uri = getattr(self, "_qr_uri", None)
        if not uri:
            raise ValueError("no top-up address yet")
        import io
        import qrcode
        import qrcode.image.svg
        buf = io.BytesIO()
        qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, border=2).save(buf)
        return buf.getvalue()

    def topup_start(self, amount: float, passphrase: str) -> dict:
        with self.lock:
            if getattr(self, "topup_proc", None) and self.topup_proc.poll() is None:
                return {"ok": False, "err": "a top-up is already waiting"}
            if not self.relayer_url():
                return {"ok": False, "err": "the relayer is off: clear the Relayer field and Save (SRNet demo relayer), then top up"}
            env = dict(os.environ)
            env[keys.PASSWORD_ENV] = passphrase
            cmd = [sys.executable, "-m", "srnet", "--home", str(self.st.root), "topup",
                   "--amount", str(amount)]
            if self.rpc_url:
                cmd += ["--rpc", self.rpc_url]
            self.topup_log = self.st.root / "topup.log"
            self.topup_log.parent.mkdir(parents=True, exist_ok=True)
            f = open(self.topup_log, "w")
            self.topup_proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=f,
                                               stderr=subprocess.STDOUT,
                                               cwd=str(Path(__file__).resolve().parents[1]))
            return {"ok": True, "started": True}

    def topup_status(self) -> dict:
        p = getattr(self, "topup_proc", None)
        if not p:
            return {"state": "idle"}
        rc = p.poll()
        tail = self.topup_log.read_text()[-1500:] if self.topup_log.exists() else ""
        tail = tail.split("ethereum:", 1)[-1] if "ethereum:" in tail else tail  # skip the ASCII QR
        return {"state": "waiting" if rc is None else ("done" if rc == 0 else "failed"), "out": tail[-800:]}

    def snapps_url(self) -> dict:
        u = self.cfg().get("snapps_url") or os.environ.get("SRNET_SNAPPS_URL") or "http://127.0.0.1:48920/"
        return {"url": u}

    def close(self) -> None:
        p = getattr(self, "topup_proc", None)
        if p and p.poll() is None:
            p.terminate()
        if self.renewer and self.renewer.poll() is None:
            self.renewer.terminate()


def _have_qr() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except ImportError:
        return False


def _ok_sesh(s) -> bool:
    return isinstance(s, str) and s.endswith(".sesh") and len(s) == 57 and s[:52].isalnum()


def make_handler(be: Backend, token: str, port_ref: list, legacy: bool = False):
    page = (WEB / "index.html").read_text().replace("@TOKEN@", token)

    class H(BaseHTTPRequestHandler):
        server_version = "srnet-ui"
        sys_version = ""

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                             "img-src 'self'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj).encode())

        def _host_ok(self) -> bool:
            if self.headers.get("Host") != f"127.0.0.1:{port_ref[0]}":
                return False
            o = self.headers.get("Origin")
            return o is None or o == f"http://127.0.0.1:{port_ref[0]}"

        def _tok(self, given) -> bool:
            return isinstance(given, str) and hmac.compare_digest(given, token)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, b"forbidden\n", "text/plain")
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path in ("/", "/app.js", "/style.css") and not legacy:
                return self._send(404, b"the wallet now lives in the SRNet app's Exits and Wallet tabs: run srnet ui\n", "text/plain")
            if u.path == "/":
                if not self._tok((q.get("k") or [None])[0]):
                    return self._send(403, b"open the link printed by srnet ui\n", "text/plain")
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if u.path in ("/app.js", "/style.css"):
                ctype = "text/javascript" if u.path.endswith(".js") else "text/css"
                return self._send(200, (WEB / u.path[1:]).read_bytes(), ctype)
            if u.path == "/qr.svg":
                if not self._tok((q.get("k") or [None])[0]):
                    return self._send(403, b"forbidden\n", "text/plain")
                try:
                    return self._send(200, be.qr_svg(), "image/svg+xml")
                except Exception as e:
                    return self._send(404, str(e).encode()[:200], "text/plain")
            return self._send(404, b"not found\n", "text/plain")

        def do_POST(self):
            if not self._host_ok() or not self._tok(self.headers.get("X-CSRF-Token")):
                return self._json(403, {"ok": False, "err": "forbidden"})
            if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
                return self._json(415, {"ok": False, "err": "json only"})
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                return self._json(413, {"ok": False, "err": "too large"})
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                return self._json(400, {"ok": False, "err": "bad json"})
            pw = req.get("passphrase") or None
            seller, cs = req.get("seller"), req.get("client_sesh")
            try:
                p = self.path
                if p == "/api/board":
                    r = be.board()
                elif p == "/api/status":
                    r = be.status(pw)
                elif p in ("/api/buy", "/api/connect"):
                    if not _ok_sesh(seller) or not _ok_sesh(cs):
                        return self._json(400, {"ok": False, "err": "seller and your .sesh address are needed"})
                    if not pw:
                        return self._json(400, {"ok": False, "err": "enter the keystore passphrase"})
                    r = (be.buy(seller, cs, bool(req.get("fund")), pw) if p == "/api/buy"
                         else be.connect(seller, cs, pw))
                elif p == "/api/autorenew":
                    on = bool(req.get("on"))
                    if on and not pw:
                        return self._json(400, {"ok": False, "err": "enter the keystore passphrase"})
                    r = be.auto_renew(on, pw)
                elif p == "/api/disconnect":
                    r = be.disconnect()
                elif p == "/api/topup/info":
                    amt = req.get("amount")
                    r = be.topup_info(float(amt) if amt else None)
                elif p == "/api/topup/start":
                    amt = float(req.get("amount") or 0)
                    if not (0 < amt <= 10_000):
                        return self._json(400, {"ok": False, "err": "enter an amount between 0 and 10000 USDC"})
                    if not pw:
                        return self._json(400, {"ok": False, "err": "enter the keystore passphrase"})
                    r = be.topup_start(amt, pw)
                elif p == "/api/topup/status":
                    r = be.topup_status()
                elif p == "/api/relayer":
                    r = be.set_relayer(req.get("url") or "")
                elif p == "/api/preflight":
                    from . import preflight
                    rep = preflight.run()
                    r = {"report": rep, "text": preflight.text(rep), "file": str(preflight.write_report(rep, home=be.st.root))}
                elif p == "/api/snapps":
                    r = be.snapps_url()
                else:
                    return self._json(404, {"ok": False, "err": "not found"})
            except Exception as e:  # report, keep serving
                return self._json(200, {"ok": False, "err": str(e)[:500]})
            r.setdefault("ok", True)
            return self._json(200, r)
    return H


def serve(be: Backend, port: int = 0, legacy: bool = False) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(24)
    port_ref = [0]
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(be, token, port_ref, legacy))
    port_ref[0] = srv.server_address[1]
    return srv, f"http://127.0.0.1:{port_ref[0]}/?k={token}"


def start_background(st: state.State, rpc_url: str | None = None) -> str:
    """Serve the wallet API on a thread for `srnet ui`; returns http://127.0.0.1:<port>/#<token>
    (the form typed.core reads from SRNET_WALLET_API)."""
    be = Backend(st, rpc_url)
    srv, url = serve(be)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base, tok = url.split("?k=", 1)
    return f"{base}#{tok}"


def run(st: state.State, rpc_url: str | None = None, open_browser: bool = True, legacy: bool = True) -> int:
    """The retired standalone page (hidden flag: srnet ui --legacy-page)."""
    be = Backend(st, rpc_url)
    srv, url = serve(be, legacy=legacy)
    print(f"srnet ui: {url}\n(local only; Ctrl-C to stop)", flush=True)
    if open_browser:
        opener = shutil.which("xdg-open") or shutil.which("open")  # Linux / macOS
        if opener:
            subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:  # Windows and anything else
            import webbrowser
            webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        be.close()
        srv.server_close()
    return 0
