"""Read-only typed views over formats that already exist: SNApp cards (Ed25519 JSON cards).
The card verifier is optional. (The exit board view moved to the exits add-on, srnet-exits.)"""
from __future__ import annotations

import importlib.util
import json
import urllib.request
from pathlib import Path

def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# SNApp card verifier: optional, not bundled in this release (drop verify_snapp_card.py
# into snapps/vendor/ to enable card views).
_CARD = Path(__file__).resolve().parent.parent / "vendor" / "verify_snapp_card.py"
cardfmt = _load("snapp_card_vendored", _CARD) if _CARD.exists() else None
MAX = 1 << 20


def _get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read(MAX + 1)


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
