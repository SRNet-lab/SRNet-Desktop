"""Spin up a full local platform for tests and demos (all on 127.0.0.1):
board host + mirror, a hub with room/wiki/shop/drop/id collections, a signed exit board
(existing format, built with the vendored exit-board code), a SNApp card, and a plain-HTML site."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mbfmt as F
from . import adapters
from .core import Core
from .host import Host, load_or_make_keys
from .hub import Hub
from .mirror import Mirror
from .node import serve, url_of

HOSTILE_HTML = """<html><head><meta http-equiv="refresh" content="0;url=http://canary/x">
<link rel=stylesheet href="http://canary/css"><style>#srnet-banner{display:none}</style>
<script>fetch('http://canary/js')</script></head><body onload="alert(1)">
<h1>Plain page</h1><p>Hello <b>world</b> <a href="javascript:alert(1)">js</a>
<a href="http://example.com/">clearnet</a> <a href="/about.html">about</a>
<img src="http://canary/img.png" alt="logo"><iframe src="http://canary/frame"></iframe>
<svg onload="alert(2)"><script>alert(3)</script></svg><form action="http://canary/f"><input name=q></form>
<object data="http://canary/o"></object><embed src="http://canary/e"></p></body></html>"""


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def static_server(directory):
    h = lambda *a, **k: _Quiet(*a, directory=str(directory), **k)  # noqa: E731
    srv = ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def make_exit_board(d: Path):
    """A signed exit board in the *existing* format (srnet-exit-board/v1, listing v2)."""
    bf, L = adapters.boardfmt, adapters.boardfmt.L
    bkey, skey = F.new_key(), F.new_key()
    now = int(time.time())
    listing = L.make(skey, payout="0x" + "11" * 20, vendor_id="0x" + "22" * 32, chainid=42161,
                     contract="0x07bbe156083ebbcc9435c721393edd36a383e648", country="NL", price_per_gb="0.50")
    body = {"v": 1, "board": L.sesh_of(bf.pub_raw(bkey)), "seq": 3, "issued": now,
            "expires": now + 3600, "max_listing_age_s": bf.DEFAULT_MAX_LISTING_AGE_S,
            "listings": [listing]}
    d.mkdir(parents=True, exist_ok=True)
    (d / "board.json").write_text(json.dumps(body))
    (d / "board.sig").write_text(bf.sign_board(bkey, body))
    return body["board"]


def start(tmp=None, admins=(), sellers=(), mirror=True, hub_override=None):
    tmp = Path(tmp or tempfile.mkdtemp(prefix="typed-"))
    env = {"tmp": tmp, "servers": []}
    # board (message board v1 codec) + mirror
    root, hk, d = load_or_make_keys(tmp / "board", "board")
    h = Host(str(tmp / "board"), "board", hk, d, {"pow_bits": 8, "new_key_pow_bits": 8})
    bs = serve(h)
    env["servers"].append(bs)
    env.update(board=h, board_url=url_of(bs), board_pin=F.pub_hex(root))
    hosts = [env["board_url"]]
    if mirror:
        m = Mirror(str(tmp / "board-mirror"), "board", F.pub_hex(root), [env["board_url"]])
        ms = serve(m)
        env["servers"].append(ms)
        h.mirrors = [url_of(ms)]
        h.run_ticker(1.0)
        hosts.append(url_of(ms))
    env["board_hosts"] = hosts
    # hub with the other typed collections
    hroot, hhk, _ = load_or_make_keys(tmp / "hub", "hub")
    low = {k: {"pow_bits": 6, "new_key_pow_bits": 8} for k in ("room", "wiki", "shop", "drop", "id")}
    for k, v in (hub_override or {}).items():
        low.setdefault(k, {}).update(v)
    hub = Hub(str(tmp / "hub"), hroot, hhk,
              [("room", "lobby"), ("wiki", "wiki"), ("shop", "store"), ("drop", "files"),
               ("id", "people")], {"admins": list(admins), "sellers": list(sellers)},
              pol_override=low)
    hs = serve(hub)
    env["servers"].append(hs)
    env.update(hub=hub, hub_url=url_of(hs), hub_pin=F.pub_hex(hroot), hub_host_key=hhk)
    # exit board + card + html site
    ex = tmp / "exit-www"
    env["exit_sesh"] = make_exit_board(ex)
    es, env["exit_url"] = static_server(ex)
    site = tmp / "site"
    site.mkdir()
    (site / "index.html").write_text(HOSTILE_HTML)
    (site / "about.html").write_text("<p>About page</p>")
    ss, env["site_url"] = static_server(site)
    env["servers"] += [es, ss]
    env["card_path"] = str(adapters._CARD.parent / "rooms.example.json")
    return env


def client_config(env):
    hub = env["hub_url"]
    cols = [{"name": "board", "kind": "board", "root_pin": env["board_pin"],
             "hosts": env["board_hosts"]}]
    for name, kind in (("lobby", "room"), ("wiki", "wiki"), ("store", "shop"), ("files", "drop"),
                       ("people", "id")):
        cols.append({"name": name, "kind": kind, "root_pin": env["hub_pin"],
                     "hosts": [f"{hub}/c/{name}"]})
    cols.append({"name": "exits", "kind": "exit", "hosts": [env["exit_url"]],
                 "pin_sesh": env["exit_sesh"]})
    cols.append({"name": "rooms-card", "kind": "card", "path": env["card_path"]})
    return {"collections": cols, "html_sites": {"demo.sesh": env["site_url"]}}


def new_core(env, name, autoconfirm=True, key_seed: bytes | None = None):
    d = env["tmp"] / "clients" / name
    d.mkdir(parents=True, exist_ok=True)
    if key_seed and not (d / "author.key").exists():
        with open(os.open(d / "author.key", os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
            f.write(key_seed)
    return Core(str(d), client_config(env), autoconfirm=autoconfirm)


def stop(env):
    for s in env["servers"]:
        try:
            s.shutdown()
            s.server_close()
        except Exception:
            pass
