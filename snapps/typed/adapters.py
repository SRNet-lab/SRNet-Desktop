"""Read-only typed views over formats that already exist: the exit board
(board.json + board.sig, srnet-exit-board/v1, unchanged) and SNApp cards (Ed25519 JSON cards).
The exit board is verified with the bundled Apache-2.0 boardfmt; the card verifier is optional."""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import urllib.request
from pathlib import Path

# The exit-board verifier is the Apache-2.0 copy bundled with the exits client
# (client-cli/vendor/boardfmt.py + listing.py).
VENDOR = Path(__file__).resolve().parents[2] / "client-cli" / "vendor"


def _load(name, path):
    d = str(path.parent)
    if d not in sys.path:
        sys.path.insert(0, d)  # boardfmt imports its sibling listing.py
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


boardfmt = _load("listing_vendored_boardfmt", VENDOR / "boardfmt.py")
# SNApp card verifier: optional, not bundled in this release (drop verify_snapp_card.py
# into snapps/vendor/ to enable card views).
_CARD = Path(__file__).resolve().parent.parent / "vendor" / "verify_snapp_card.py"
cardfmt = _load("snapp_card_vendored", _CARD) if _CARD.exists() else None
MAX = 1 << 20


def _get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(MAX + 1)


def exit_view(base_url: str, pin_sesh: str, seen: dict | None = None, now=None) -> dict:
    """Fetch and verify an exit board.  Returns typed exit.listing items for the template."""
    body = _get(base_url.rstrip("/") + "/board.json")
    sig = _get(base_url.rstrip("/") + "/board.sig").decode().strip()
    b, good, dropped = boardfmt.verify_board(body, sig, pin_sesh, now=now, seen=seen)
    items = []
    for doc in good:
        l = doc["listing"]
        items.append({"t": "exit.listing", "seller": l.get("seller"),
                      "fields": {k: v for k, v in l.items()
                                 if isinstance(v, (str, int, float, bool)) and k != "seller"}})
    return {"board": b["board"], "seq": b["seq"], "issued": b["issued"], "expires": b["expires"],
            "listings": items, "dropped": dropped,
            "seen": {"seq": b["seq"], "hash": boardfmt.body_hash(b)}}


def card_view(card: dict, expect_pubkey: str | None = None) -> dict:
    """Verify a SNApp card; returns a typed snapp.card view (shown before opening a service)."""
    if cardfmt is None:
        return {"t": "snapp.card", "ok": False, "errors": ["card verifier not bundled"],
                "card": {}, "pubkey": card.get("pubkey")}
    errs = cardfmt.verify(card, expect_pubkey=expect_pubkey)
    shown = {k: card.get(k) for k in ("snapp_id", "ver", "host_sesh", "port", "peer_id",
                                      "issued_at", "expires_at", "key_id")}
    return {"t": "snapp.card", "ok": not errs, "errors": errs, "card": shown,
            "pubkey": card.get("pubkey")}


def card_from_url(url, expect_pubkey=None):
    return card_view(json.loads(_get(url)), expect_pubkey)
