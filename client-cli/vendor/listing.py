#!/usr/bin/env python3
"""Signed exit listing (nothing here publishes anything).

A listing is a small JSON offer signed with the exit's own identity key, so a buyer can
check that the offer really comes from the .sesh it names (the .sesh IS the Ed25519 pubkey,
z-base32 encoded).

  listing.py make   --identity exit.key --price-per-hour-usdc 0.50 [--max-hours 168] \
                    --payout 0x... --vendor-id 0x... --chainid 84532 --contract 0x... \
                    --region "US-Central" [--policy tcp/443 --policy tcp/80] > listing.json
  listing.py verify listing.json        # exit 0 if the signature matches the named .sesh
  listing.py refresh --identity exit.key --listing listing.json [--submit-url URL]
                                        # re-sign with a new 'issued' time (same offer) and,
                                        # if a board submit URL is set, resubmit it

Liveness: the seller pack refreshes the listing every hour (srnet-seller-listing.timer). Boards
hide a listing that has not been refreshed for 3 hours and show it again on the next valid
refresh, as long as the payout fields are unchanged.

Listing body ("v":2, everything priced by the hour):
  {"v":2, "seller":"<.sesh>", "issued":<unix>, "price_per_hour_usdc":"0.50", "max_hours":168,
   "payout":"0x..", "vendor_id":"0x..", "chainid":N, "contract":"0x..", "region":"..", "policy":[..]}
Buyers pick whole hours (>= 1); lease amount = price_per_hour x hours, capped at $20 by the contract.

identity file: the session-router keyfile (128 hex chars = 32-byte seed || 32-byte pubkey).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ZB32 = "ybndrfg8ejkmcpqxot1uwisza345h769"
DOMAIN = "srnet-exit-listing/v1"


def zb32_encode(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    n = len(data) * 8
    pad = (5 - n % 5) % 5
    bits <<= pad
    n += pad
    return "".join(ZB32[(bits >> (n - 5 * (i + 1))) & 31] for i in range(n // 5))


def zb32_decode(s: str, nbytes: int = 32) -> bytes:
    bits = 0
    for ch in s:
        bits = (bits << 5) | ZB32.index(ch)
    extra = len(s) * 5 - nbytes * 8
    if extra < 0:
        raise ValueError("too short")
    if bits & ((1 << extra) - 1):
        raise ValueError("non-canonical z-base32")
    return (bits >> extra).to_bytes(nbytes, "big")


def sesh_of(pub: bytes) -> str:
    return zb32_encode(pub) + ".sesh"


def pub_of_sesh(sesh: str) -> bytes:
    if not sesh.endswith(".sesh") or len(sesh) != 57:
        raise ValueError("bad .sesh")
    return zb32_decode(sesh[:-5])


def load_identity(path: str | Path) -> Ed25519PrivateKey:
    raw = bytes.fromhex(Path(path).read_text().strip())
    if len(raw) not in (32, 64):
        raise ValueError("identity must be 32-byte seed or 64-byte seed||pubkey hex")
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    if len(raw) == 64:
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        if pub != raw[32:]:
            raise ValueError("identity file pubkey half does not match seed")
    return key


def canonical(body: dict) -> bytes:
    return (DOMAIN + "\n" + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()


def make(key: Ed25519PrivateKey, **fields) -> dict:
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    body = {"v": 2, "seller": sesh_of(pub), "issued": int(time.time()), **fields}
    return {"listing": body, "sig": key.sign(canonical(body)).hex()}


def verify(doc: dict) -> bool:
    try:
        body = doc["listing"]
        pub = pub_of_sesh(body["seller"])
        Ed25519PublicKey.from_public_bytes(pub).verify(bytes.fromhex(doc["sig"]), canonical(body))
        return True
    except (KeyError, ValueError, InvalidSignature, TypeError):
        return False


def expires_at(body: dict, max_age_s: int) -> int:
    """Effective expiry: the listing's own 'expires' (if any), capped at issued + max_age_s."""
    cap = int(body.get("issued", 0)) + int(max_age_s)
    exp = body.get("expires")
    return min(int(exp), cap) if isinstance(exp, int) else cap


REFRESH_INTERVAL_S = 3600
SUBMIT_TIMEOUT_S = 30


def refresh(key: Ed25519PrivateKey, path: str | Path) -> dict:
    """Re-sign the listing at `path` with a fresh 'issued' time; the offer itself is unchanged."""
    path = Path(path)
    old = json.loads(path.read_text())
    body = dict(old["listing"])
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if body.get("seller") != sesh_of(pub):
        raise ValueError("listing seller does not match this identity key")
    fields = {k: v for k, v in body.items() if k not in ("v", "seller", "issued")}
    doc = make(key, **fields)
    if not verify(doc):
        raise ValueError("refreshed listing failed to verify")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".listing-")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(doc, indent=1) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return doc


def submit(doc: dict, url: str) -> str:
    if not url.startswith(("http://", "https://")):
        raise ValueError("submit URL must be http(s)")
    req = urllib.request.Request(url, data=json.dumps(doc).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=SUBMIT_TIMEOUT_S) as r:
        return f"{r.status} {r.read(200).decode(errors='replace').strip()}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--identity", required=True)
    m.add_argument("--price-per-hour-usdc", "--price-usdc", dest="price_per_hour_usdc", required=True,
                   help="USDC per hour, e.g. 0.50")
    m.add_argument("--max-hours", type=int, default=168)
    m.add_argument("--payout", required=True)
    m.add_argument("--vendor-id", required=True)
    m.add_argument("--chainid", type=int, required=True)
    m.add_argument("--contract", required=True)
    m.add_argument("--region", default="")
    m.add_argument("--policy", action="append", default=[])
    v = sub.add_parser("verify")
    v.add_argument("file")
    r = sub.add_parser("refresh")
    r.add_argument("--identity", required=True)
    r.add_argument("--listing", required=True)
    r.add_argument("--submit-url", default=os.environ.get("BOARD_SUBMIT_URL", ""),
                   help="board submit endpoint (env BOARD_SUBMIT_URL); empty = only re-sign locally")
    a = ap.parse_args(argv)
    if a.cmd == "refresh":
        doc = refresh(load_identity(a.identity), a.listing)
        print(f"refreshed {doc['listing']['seller']} issued={doc['listing']['issued']}")
        if a.submit_url:
            try:
                print("board:", submit(doc, a.submit_url))
            except Exception as e:  # network errors: keep the fresh local listing, report failure
                print(f"board submit failed: {e}", file=sys.stderr)
                return 3
        return 0
    if a.cmd == "make":
        doc = make(load_identity(a.identity), price_per_hour_usdc=a.price_per_hour_usdc, max_hours=a.max_hours,
                   payout=a.payout.lower(),
                   vendor_id=a.vendor_id.lower(), chainid=a.chainid, contract=a.contract.lower(),
                   region=a.region, policy=a.policy or ["tcp/443", "tcp/80"])
        print(json.dumps(doc, indent=1))
        return 0
    ok = verify(json.loads(Path(a.file).read_text()))
    print("valid" if ok else "INVALID")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
