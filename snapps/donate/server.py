#!/usr/bin/env python3
"""Donation page: the donor picks ETH or USDC, a chain and any amount; the page answers with the
address, an exact amount of its own and a QR code (EIP-681), and the donor pays from their own wallet.
After paying, the donor pastes the transaction hash and the page checks it on chain.

Plain HTML forms, no scripts. Loopback only; reach it through the store's .sesh front
(route /donate). Standard library only; QR codes appear if the `qrcode` package is installed.

    python3 -m donate.server --data DIR --pay-to 0x... [--bind 127.0.0.1] [--port 48922]
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from donate import pay as P  # noqa: E402

PREFIX = "/donate"
ID_RE = re.compile(r"^[a-z2-7]{26}$")
MAX_BODY = 8192
MAX_OPEN = 5000
KEEP_S = 30 * 86400        # unpaid donations are forgotten this long after opening
NOT_AFFILIATED = "SRNet is not affiliated with the Session Foundation or the Session Technology Foundation."
CSP = ("default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'")
CSS = (b"body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:2em auto;padding:0 1em;color:#111}"
       b"code,.mono{font-family:ui-monospace,monospace;overflow-wrap:anywhere}"
       b".amount{font-size:1.4em;font-weight:600}.box{border:1px solid #ccc;border-radius:6px;padding:1em;margin:1em 0}"
       b".ok{color:#065f2c;font-weight:600}.warn{color:#8a3b00}small{color:#555}"
       b"input[type=text]{width:100%;box-sizing:border-box;padding:.4em;font:inherit}"
       b"button{font:inherit;padding:.4em 1em}img{max-width:260px}")


def new_id() -> str:
    import base64
    return base64.b32encode(secrets.token_bytes(16)).decode().lower().rstrip("=")[:26]


class Ledger:
    """Donations on disk (one JSON file, replaced atomically). Every change happens under one lock."""

    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock, self.lock = Path(path), clock, threading.RLock()
        self.d = {"donations": {}, "used_tx": {}}
        if self.path.exists():
            # Refuse to start on an unreadable ledger rather than start empty: an empty ledger would
            # forget which transactions already paid a donation.
            try:
                with open(self.path) as f:
                    got = json.load(f)
                ok = isinstance(got.get("donations"), dict) and isinstance(got.get("used_tx"), dict)
            except (OSError, ValueError, AttributeError):
                ok = False
            if not ok:
                raise SystemExit(f"{self.path} is unreadable; fix or move it away first")
            self.d = got

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.d, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            try:   # make the rename itself durable
                dfd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _prune(self, now):
        ds = self.d["donations"]
        for k in [k for k, v in ds.items() if v["status"] != "paid" and now - v["created"] > KEEP_S]:
            del ds[k]

    def is_open(self, v, now) -> bool:
        # The amount stays reserved a little past the 7 days, so a later donation that reuses it opens
        # more than EARLY_SLACK_S after any payment that could still count for this one.
        return v["status"] != "paid" and now - v["created"] < P.OPEN_S + P.EARLY_SLACK_S

    def open(self, asset: str, chain: int, base: int) -> dict:
        with self.lock:
            now = self.clock()
            self._prune(now)
            live = [v for v in self.d["donations"].values() if self.is_open(v, now)]
            if len(live) >= MAX_OPEN:
                raise P.Busy("too many open donations right now")
            taken = {v["units"] for v in live if v["asset"] == asset and v["chain"] == chain}
            units = P.unique_units(asset, base, taken)
            did = new_id()
            while did in self.d["donations"]:
                did = new_id()
            v = {"id": did, "asset": asset, "chain": chain, "units": units, "asked": base,
                 "created": int(now), "status": "open"}
            self.d["donations"][did] = v
            self._save()
            return dict(v)

    def get(self, did: str) -> dict | None:
        with self.lock:
            v = self.d["donations"].get(did)
            return dict(v) if v else None

    def note(self, did: str, msg: str):
        with self.lock:
            v = self.d["donations"].get(did)
            if v and v["status"] != "paid":
                v["last_check"] = msg[:300]
                self._save()

    def mark_paid(self, did: str, tx: str, res: dict) -> dict:
        """Record a checked payment. The same transaction can pay only one donation."""
        with self.lock:
            v = self.d["donations"].get(did)
            if not v:
                return {"state": "bad", "why": "unknown donation"}
            if v["status"] == "paid":
                return {"state": "paid", "already": True}
            key = f"{v['chain']}:{tx.lower()}"
            other = self.d["used_tx"].get(key)
            if other and other != did:
                return {"state": "bad", "why": "that transaction was already used for another donation"}
            v.update(status="paid", tx=tx.lower(), sender=res.get("from"), block=res.get("block"),
                     block_hash=res.get("block_hash"), mined=res.get("time"), paid_at=int(self.clock()))
            v.pop("last_check", None)
            self.d["used_tx"][key] = did
            self._save()
            return {"state": "paid"}


class RateLimit:
    def __init__(self, per_caller: int, total: int, window: float, clock=time.time):
        self.per, self.total, self.win, self.clock = per_caller, total, window, clock
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, who: str) -> bool:
        with self.lock:
            now = self.clock()
            for k in list(self.hits):
                self.hits[k] = [t for t in self.hits[k] if now - t < self.win]
                if not self.hits[k]:
                    del self.hits[k]
            if sum(len(v) for v in self.hits.values()) >= self.total or len(self.hits.get(who, ())) >= self.per:
                return False
            self.hits.setdefault(who, []).append(now)
            return True


class App:
    def __init__(self, data: Path, pay_to: str, rpcs: dict | None = None, quorum: int | None = None,
                 clock=time.time):
        if not P.ADDR_RE.match(pay_to):
            raise SystemExit("--pay-to must be a 0x address")
        self.pay_to, self.clock, self.quorum = pay_to, clock, quorum
        self.ledger = Ledger(Path(data) / "donations.json", clock)
        self.rpcs = rpcs or {c: [P.JsonRpc(u) for u in v["rpcs"]] for c, v in P.CHAINS.items()}
        self.create_rl = RateLimit(20, 300, 3600, clock)
        self.check_rl = RateLimit(20, 120, 600, clock)
        self.donation_rl = RateLimit(3, 10**9, 60, clock)   # checks per donation
        self.cache = P.TxCache(30, clock=clock)
        self.check_locks = {c: threading.Lock() for c in self.rpcs}

    # ---- actions shared by the HTML and JSON views ----
    def create(self, asset: str, chain, amount: str, who: str) -> dict:
        try:
            chain = int(chain)
        except (TypeError, ValueError):
            raise P.AmountError("pick a network") from None
        if not P.offered(asset, chain):
            raise P.AmountError("that asset isn't offered on that network")
        base = P.parse_amount(amount, asset)
        if not self.create_rl.allow(who):
            raise P.Busy("too many new donations just now")
        return self.ledger.open(asset, chain, base)

    def sheet(self, v: dict) -> dict:
        a, c = P.ASSETS[v["asset"]], P.CHAINS[v["chain"]]
        out = {"id": v["id"], "network": c["label"], "chain_id": v["chain"], "asset": a["name"],
               "to": self.pay_to, "amount": P.fmt_units(v["units"], v["asset"]), "units": str(v["units"]),
               "uri": P.pay_uri(v["asset"], v["chain"], self.pay_to, v["units"]),
               "status": v["status"], "opened": v["created"],
               "open_until": v["created"] + P.OPEN_S}
        if v["asset"] == "usdc":
            out["token"] = P.USDC_TOKENS[v["chain"]]
        if v["status"] == "paid":
            out.update(tx=v["tx"], link=c["explorer"] + v["tx"], block=v["block"], sender=v.get("sender"))
        elif v.get("last_check"):
            out["last_check"] = v["last_check"]
        return out

    def any_amount(self, asset: str, chain: int) -> dict:
        """Address and QR without an exact amount, for when no new donation can be opened."""
        out = {"network": P.CHAINS[chain]["label"], "chain_id": chain, "asset": P.ASSETS[asset]["name"],
               "to": self.pay_to, "uri": P.any_uri(asset, chain, self.pay_to),
               "note": "send any amount; this page can't confirm it right now"}
        if asset == "usdc":
            out["token"] = P.USDC_TOKENS[chain]
        return out

    def check(self, did: str, tx: str, who: str) -> dict:
        v = self.ledger.get(did)
        if not v:
            return {"state": "bad", "why": "unknown donation"}
        if v["status"] == "paid":
            return {"state": "paid", "already": True}
        tx = str(tx or "").strip()
        if not P.TX_RE.match(tx):
            return {"state": "bad", "why": "paste the transaction hash: 0x and 64 hex characters"}
        # No expiry refusal here: a payment mined within the donation's 7 days still counts when it is
        # checked later (the chain check enforces the window).
        with self.ledger.lock:
            other = self.ledger.d["used_tx"].get(f"{v['chain']}:{tx.lower()}")
        if other and other != did:
            return {"state": "bad", "why": "that transaction was already used for another donation"}
        if not self.donation_rl.allow(did):
            return {"state": "pending", "why": "this donation was checked a few times just now; try again in a minute"}
        if not self.check_rl.allow(who):
            return {"state": "pending", "why": "too many checks just now; try again in a few minutes"}
        with self.check_locks[v["chain"]]:   # one check per chain at a time keeps the public RPCs happy
            res = P.check(self.rpcs[v["chain"]], dict(v, tx=tx), self.pay_to, self.quorum, self.cache)
        if res["state"] == "paid":
            got = self.ledger.mark_paid(did, tx, res)
            if got["state"] != "paid":
                return got
            return {"state": "paid"}
        self.ledger.note(did, res.get("why", ""))
        return res


def qr_svg(data: str) -> bytes | None:
    try:
        import io
        import qrcode
        import qrcode.image.svg
    except ImportError:
        return None
    buf = io.BytesIO()
    qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2).save(buf)
    return buf.getvalue()


E = html.escape


def page(title: str, body: str) -> bytes:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<link rel=\"stylesheet\" href=\"{PREFIX}/style.css\"><title>{E(title)}</title></head><body>"
            f"{body}<hr><p><small>{E(NOT_AFFILIATED)}</small></p></body></html>").encode()


def index_html(app: App, err: str = "", form: dict | None = None) -> bytes:
    form = form or {}
    opts = []
    for asset, chain in P.OPTIONS:
        val = f"{asset}:{chain}"
        sel = " checked" if form.get("what") == val or (not form.get("what") and val == "usdc:42161") else ""
        lab = f"{P.ASSETS[asset]['name']} on {P.CHAINS[chain]['label']}"
        opts.append(f"<label><input type=\"radio\" name=\"what\" value=\"{val}\"{sel}> {E(lab)}</label><br>")
    lim = "; ".join(f"{a['name']} {a['min']} to {a['max']}" for a in P.ASSETS.values())
    body = ("<h1>Donate to SRNet</h1>"
            "<p>Donations support SRNet development. You get nothing in return: no goods and no services. "
            "Payments are final.</p>"
            f"<p>Every donation goes to the SRNet wallet <code>{E(app.pay_to)}</code>.</p>"
            + (f"<p class=\"warn\">{E(err)}</p>" if err else "")
            + f"<form method=\"post\" action=\"{PREFIX}/new\" class=\"box\"><p><b>What you'll send</b><br>"
            + "".join(opts) + "</p>"
            f"<p><label><b>Amount</b><br><input type=\"text\" name=\"amount\" inputmode=\"decimal\" "
            f"value=\"{E(form.get('amount', ''))}\" placeholder=\"for example 5 or 0.002\" required></label><br>"
            f"<small>{E(lim)}.</small></p><p><button type=\"submit\">Show how to pay</button></p></form>"
            "<p><small>The next page shows an exact amount, up to 0.01 USDC (or 0.00001 ETH) more than "
            "you typed. That small extra is how this page tells your donation apart "
            "from others, so please send exactly that amount. You pay from your own wallet and you pay the "
            "network fee.</small></p>")
    return page("Donate to SRNet", body)


def donation_html(app: App, v: dict, msg: str = "", ok: bool = False) -> bytes:
    s = app.sheet(v)
    if s["status"] == "paid":
        body = (f"<h1>Thank you</h1><p class=\"ok\">Received {E(s['amount'])} {E(s['asset'])} on "
                f"{E(s['network'])}.</p><p>Transaction: <a href=\"{E(s['link'])}\" class=\"mono\">{E(s['tx'])}</a>"
                f"</p><p><small>Block {s['block']}. Payments are final.</small></p>")
        return page("Thank you", body)
    qr = f"<p><img src=\"{PREFIX}/d/{s['id']}/qr.svg\" alt=\"QR code for this payment\"></p>" \
        if have_qr() else ""
    tok = (f"<p>Token: USDC <code>{E(s['token'])}</code> (the official one on {E(s['network'])}). "
           "Send the token, not ETH.</p>") if "token" in s else \
        f"<p>Send native ETH on {E(s['network'])}, not a token.</p>"
    until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(s["open_until"]))
    note = msg or s.get("last_check", "")
    body = (f"<h1>Donate {E(s['asset'])} on {E(s['network'])}</h1><div class=\"box\">"
            f"<p>Send exactly<br><span class=\"amount mono\">{E(s['amount'])} {E(s['asset'])}</span></p>"
            f"<p>to<br><code>{E(s['to'])}</code><br>on <b>{E(s['network'])}</b> (chain id {s['chain_id']}).</p>"
            + tok + qr +
            f"<p><a href=\"{E(s['uri'])}\">Open in a wallet</a> <small class=\"mono\">{E(s['uri'])}</small></p></div>"
            "<p><small>Send it from a regular wallet. ETH forwarded by a smart-contract wallet can't be "
            "checked here, although it still arrives.</small></p>"
            + (f"<p class=\"{'ok' if ok else 'warn'}\">{E(note)}</p>" if note else "")
            + f"<form method=\"post\" action=\"{PREFIX}/d/{s['id']}/check\" class=\"box\">"
            "<p><label><b>After you pay</b>, paste the transaction hash<br>"
            "<input type=\"text\" name=\"tx\" placeholder=\"0x...\" required></label></p>"
            "<p><button type=\"submit\">Check payment</button></p></form>"
            f"<p><small>This amount is held for you until {until}. Bookmark this page to check later."
            "</small></p>")
    return page("Donate to SRNet", body)


def have_qr() -> bool:
    try:
        import qrcode  # noqa: F401
        return True
    except ImportError:
        return False


def any_html(app: App, asset: str, chain: int, why: str) -> bytes:
    s = app.any_amount(asset, chain)
    qr = (f"<p><img src=\"{PREFIX}/any/{asset}/{chain}/qr.svg\" alt=\"QR code for the donation address\"></p>"
          if have_qr() else "")
    tok = (f"<p>Token: USDC <code>{E(s['token'])}</code> (the official one on {E(s['network'])}). "
           "Send the token, not ETH.</p>") if "token" in s else \
        f"<p>Send native ETH on {E(s['network'])}, not a token.</p>"
    body = (f"<h1>Donate {E(s['asset'])} on {E(s['network'])}</h1>"
            f"<p class=\"warn\">This page can't open a new donation right now ({E(why)}). You can still send "
            "any amount to the address below; this page just can't confirm it right now.</p><div class=\"box\">"
            f"<p>Send any amount to<br><code>{E(s['to'])}</code><br>on <b>{E(s['network'])}</b> "
            f"(chain id {s['chain_id']}).</p>" + tok + qr +
            f"<p><a href=\"{E(s['uri'])}\">Open in a wallet</a> <small class=\"mono\">{E(s['uri'])}</small></p></div>"
            "<p><small>Payments are final.</small></p>")
    return page("Donate to SRNet", body)


NOT_FOUND = page("Not found", f"<p>No such donation (it may have expired). <a href=\"{PREFIX}/\">Start again</a>.</p>")


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30   # an idle or slow connection doesn't hold a thread forever

        def version_string(self):
            return "srnet-donate"

        def log_message(self, *a):
            pass

        def who(self) -> str:
            return (self.headers.get("X-Forwarded-For") or self.client_address[0])[:64]

        def send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if code in (400, 413):
                # The request may not have been read to its end; never parse what's left as a new request.
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, code: int, obj):
            self.send(code, json.dumps(obj).encode(), "application/json")

        def redirect(self, to: str):
            self.send(303, b"", "text/plain", {"Location": to})

        def body(self) -> bytes | None:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if n < 0 or n > MAX_BODY:
                return None
            return self.rfile.read(n) if n else b""

        def path_parts(self):
            p = urllib.parse.urlsplit(self.path).path
            if p != PREFIX and not p.startswith(PREFIX + "/"):
                return None
            return p[len(PREFIX):].strip("/")

        def do_GET(self):
            if self.headers.get("Content-Length", "0").strip() not in ("", "0") or self.headers.get("Transfer-Encoding"):
                return self.send(400, b"bad request", "text/plain")   # a GET with a body: don't guess
            p = self.path_parts()
            if p is None:
                return self.send(404, b"not found", "text/plain")
            if p == "":
                return self.send(200, index_html(app))
            if p == "style.css":
                return self.send(200, CSS, "text/css")
            m = re.fullmatch(r"any/(eth|usdc)/([0-9]{1,6})/qr\.svg", p)
            if m and P.offered(m.group(1), int(m.group(2))):
                svg = qr_svg(P.any_uri(m.group(1), int(m.group(2)), app.pay_to))
                return self.send(200, svg, "image/svg+xml") if svg else self.send(404, b"no qr", "text/plain")
            m = re.fullmatch(r"(api/)?d/([a-z2-7]{26})(/qr\.svg)?", p)
            if not m:
                return self.send(404, b"not found", "text/plain")
            v = app.ledger.get(m.group(2))
            if not v:
                if m.group(1):
                    return self.json(404, {"error": "unknown donation"})
                return self.send(404, NOT_FOUND)
            if m.group(3):
                svg = qr_svg(app.sheet(v)["uri"])
                return self.send(200, svg, "image/svg+xml") if svg else self.send(404, b"no qr", "text/plain")
            if m.group(1):
                return self.json(200, app.sheet(v))
            return self.send(200, donation_html(app, v))

        do_HEAD = do_GET

        def do_POST(self):
            p = self.path_parts()
            if self.headers.get("Transfer-Encoding"):
                return self.send(400, b"bad request", "text/plain")
            raw = self.body()
            if p is None:
                return self.send(404, b"not found", "text/plain")
            if raw is None:
                return self.send(413, b"too large", "text/plain")
            is_api = p.startswith("api/")
            try:
                if is_api:
                    a = json.loads(raw or b"{}")
                    if not isinstance(a, dict):
                        raise ValueError
                    a = {k: str(v) for k, v in a.items() if isinstance(v, (str, int))}
                else:
                    a = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"),
                                                                   max_num_fields=8).items()}
            except ValueError:
                return self.send(400, b"bad request", "text/plain")
            if p in ("new", "api/new"):
                if is_api:
                    asset, chain = a.get("asset", ""), a.get("chain_id", "")
                else:
                    asset, _, chain = a.get("what", "").partition(":")
                try:
                    v = app.create(asset, chain, a.get("amount", ""), self.who())
                except P.Busy as e:   # donating never gets blocked: show the plain address instead
                    c = int(chain)
                    return self.json(503, {"error": str(e), "busy": True, "any_amount": app.any_amount(asset, c)}) \
                        if is_api else self.send(200, any_html(app, asset, c, str(e)))
                except P.AmountError as e:
                    return self.json(400, {"error": str(e)}) if is_api else \
                        self.send(400, index_html(app, str(e), a))
                return self.json(200, app.sheet(v)) if is_api else self.redirect(f"{PREFIX}/d/{v['id']}")
            m = re.fullmatch(r"(api/)?d/([a-z2-7]{26})/check", p)
            if not m:
                return self.send(404, b"not found", "text/plain")
            res = app.check(m.group(2), a.get("tx", ""), self.who())
            if is_api:
                v = app.ledger.get(m.group(2))
                return self.json(200, dict(res, donation=app.sheet(v) if v else None))
            v = app.ledger.get(m.group(2))
            if not v:
                return self.send(404, NOT_FOUND)
            return self.send(200, donation_html(app, v, res.get("why", ""), res["state"] == "paid"))

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    ap.add_argument("--pay-to", required=True)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48922)
    a = ap.parse_args(argv)
    app = App(Path(a.data), a.pay_to)

    class S(ThreadingHTTPServer):
        daemon_threads = True

    srv = S((a.bind, a.port), make_handler(app))
    print(f"srnet-donate on {a.bind}:{a.port}{PREFIX}/ paying to {a.pay_to}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
