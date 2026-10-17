"""Generic typed record envelope and per-collection codecs.

Typed envelope:
  {"rec": {"t": "room.msg", "v": 1, "c": "<collection>", "author": "<hex Ed25519>",
           "ts": 1792000000, "nonce": "<pow salt>", "body": {...}, "refs": ["<id>", ...]},
   "sig": "<Ed25519 over 'srnet-typed/<t>/v<v>\\n' + canonical(rec)>", "id": "<id>"}

- Per-type signing domain, so a signature for one type can never be replayed as another.
- `c` binds the record to one collection (no cross-collection replay).
- id = z-base32(SHA-256(signed bytes)[:16]); proof of work = leading zero bits of
  SHA-256("srnet-typed/pow/v1\\n" + canonical(rec)), searched by varying `nonce`.
- Bodies never contain HTML; types declare their body schema below.

A codec turns an envelope into verified Meta for one collection.  BoardCodec keeps the
message board's v1 wire format (srnet-msgboard-post/v1) so existing boards keep verifying;
TypedCodec handles everything new.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field

import mbfmt as F

TYPED_POW_DOMAIN = "srnet-typed/pow/v1"
NAME_RE = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$")
ID_RE = re.compile("^[ybndrfg8ejkmcpqxot1uwisza345h769]{26}$")
REC_FIELDS = {"t", "v", "c", "author", "ts", "nonce", "body", "refs"}
MAX_REC_BYTES = 8192


def domain(t: str, v: int) -> str:
    return f"srnet-typed/{t}/v{v}"


@dataclass
class Meta:
    id: str
    t: str
    author: str
    ts: int
    ref: str | None = None  # first ref (reply target) for indexing
    body: dict = field(default_factory=dict)
    refs: list = field(default_factory=list)


# ---- body field helpers ---------------------------------------------------------------
def text(max_chars: int, required=True, multiline=True):
    def chk(v):
        if v is None and not required:
            return
        if not isinstance(v, str) or len(v) > max_chars or (required and not v.strip()):
            raise F.FormatError(f"text length (max {max_chars})")
        if unicodedata.normalize("NFC", v) != v:
            raise F.FormatError("text not NFC")
        bad = "\n\t" if multiline else "\t"
        if any(unicodedata.category(ch) == "Cc" and ch not in bad for ch in v):
            raise F.FormatError("control characters")
    return chk


def hexhash(required=False):
    def chk(v):
        if v is None and not required:
            return
        if not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v):
            raise F.FormatError("hash")
    return chk


@dataclass
class TypeSpec:
    name: str
    v: int
    fields: dict            # body field -> checker
    max_refs: int = 1
    pow_extra: int = 0      # extra PoW bits on top of the collection policy

    @property
    def key(self):
        return f"{self.name}@{self.v}"

    def check_body(self, body, refs):
        if not isinstance(body, dict) or set(body) - set(self.fields):
            raise F.FormatError(f"{self.key}: unexpected body fields")
        for k, chk in self.fields.items():
            chk(body.get(k))
        if not isinstance(refs, list) or len(refs) > self.max_refs or \
                not all(isinstance(r, str) and ID_RE.match(r) for r in refs):
            raise F.FormatError(f"{self.key}: refs")


class TypeSet:
    def __init__(self, *specs: TypeSpec):
        self.specs = {s.key: s for s in specs}

    def get(self, t, v) -> TypeSpec:
        s = self.specs.get(f"{t}@{v}")
        if s is None:
            raise F.FormatError(f"unknown type {t}@{v} for this collection")
        return s


# ---- the first typed types -------------------------------------------------
ROOM_MSG = TypeSpec("room.msg", 1, {"text": text(500)}, max_refs=1)
ROOM_TOPIC = TypeSpec("room.topic", 1, {"text": text(200, multiline=False)}, max_refs=0)
ID_PROFILE = TypeSpec("id.profile", 1, {"name": text(32, multiline=False),
                                        "bio": text(280, required=False),
                                        "avatar_sha256": hexhash()}, max_refs=0, pow_extra=2)
ROOM_TYPES = TypeSet(ROOM_MSG, ROOM_TOPIC, ID_PROFILE)


# ---- typed records ---------------------------------------------------------------------
def _pow_bits_of(rec: dict) -> int:
    d = hashlib.sha256(F.canonical(TYPED_POW_DOMAIN, rec)).digest()
    n = 0
    for b in d:
        if b == 0:
            n += 8
            continue
        return n + (8 - b.bit_length())
    return n


def record_id(rec: dict) -> str:
    return F.zb32(hashlib.sha256(F.canonical(domain(rec["t"], rec["v"]), rec)).digest()[:16])


def make_record(key, types: TypeSet, coll: str, t: str, body: dict, refs=None, v: int = 1,
                ts: int | None = None, pow_bits: int = 0) -> dict:
    spec = types.get(t, v)
    body = {k: (unicodedata.normalize("NFC", x) if isinstance(x, str) else x)
            for k, x in body.items() if x is not None}
    rec = {"t": t, "v": v, "c": coll, "author": F.pub_hex(key),
           "ts": int(time.time()) if ts is None else ts, "nonce": "", "body": body,
           "refs": list(refs or [])}
    spec.check_body(body, rec["refs"])
    need = pow_bits + spec.pow_extra
    if need:
        pre, i = os.urandom(4).hex(), 0
        while True:
            rec["nonce"] = f"{pre}{i:x}"
            if _pow_bits_of(rec) >= need:
                break
            i += 1
    sig = key.sign(F.canonical(domain(t, v), rec)).hex()
    return {"rec": rec, "sig": sig, "id": record_id(rec)}


class TypedCodec:
    kind = "typed"

    def __init__(self, types: TypeSet):
        self.types = types

    def verify(self, env, coll, pow_bits=0, now=None, check_time=False) -> Meta:
        rec = env.get("rec") if isinstance(env, dict) else None
        if not isinstance(rec, dict) or set(rec) != REC_FIELDS:
            raise F.FormatError("record fields")
        if not isinstance(rec["t"], str) or not NAME_RE.match(rec["t"]) or \
                not isinstance(rec["v"], int) or isinstance(rec["v"], bool):
            raise F.FormatError("type name/version")
        spec = self.types.get(rec["t"], rec["v"])
        if coll is not None and rec["c"] != coll:
            raise F.FormatError("wrong collection")
        if not isinstance(rec["author"], str) or not F.HEX64.match(rec["author"]):
            raise F.FormatError("author")
        if not isinstance(rec["ts"], int) or isinstance(rec["ts"], bool) or rec["ts"] < 0:
            raise F.FormatError("ts")
        if not isinstance(rec["nonce"], str) or len(rec["nonce"]) > 64:
            raise F.FormatError("nonce")
        spec.check_body(rec["body"], rec["refs"])
        msg = F.canonical(domain(rec["t"], rec["v"]), rec)
        if len(msg) > MAX_REC_BYTES:
            raise F.FormatError("too large")
        if not F._verify(rec["author"], env.get("sig"), msg):
            raise F.FormatError("bad signature")
        rid = record_id(rec)
        if "id" in env and env["id"] != rid:
            raise F.FormatError("id mismatch")
        if check_time:
            now = time.time() if now is None else now
            if rec["ts"] > now + 60 or rec["ts"] < now - F.CLOCK_SKEW_S:
                raise F.FormatError("stale or future timestamp")
        if (pow_bits or spec.pow_extra) and _pow_bits_of(rec) < pow_bits + spec.pow_extra:
            raise F.FormatError("insufficient proof of work")
        return Meta(rid, rec["t"], rec["author"], rec["ts"],
                    rec["refs"][0] if rec["refs"] else None, rec["body"], rec["refs"])

    def normalize(self, env):
        return {"rec": env["rec"], "sig": env["sig"], "id": record_id(env["rec"])}


class BoardCodec:
    """Message board v1 posts (srnet-msgboard-post/v1), exposed to templates as board.post@1."""
    kind = "board"

    def verify(self, env, coll, pow_bits=0, now=None, check_time=False) -> Meta:
        pid = F.verify_post(env, board=coll, now=now, pow_bits=pow_bits, check_time=check_time)
        p = env["post"]
        return Meta(pid, "board.post", p["author"], p["ts"], p["reply_to"],
                    {"text": p["body"], "reply_to": p["reply_to"]},
                    [p["reply_to"]] if p["reply_to"] else [])

    def normalize(self, env):
        return {"post": env["post"], "sig": env["sig"], "id": F.post_id(env["post"])}


BOARD = BoardCodec()


# ---- more field checkers -----------------------------------------------------------------
def integer(lo, hi, required=True):
    def chk(v):
        if v is None and not required:
            return
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise F.FormatError(f"integer out of range {lo}..{hi}")
    return chk


def boolean():
    def chk(v):
        if not isinstance(v, bool):
            raise F.FormatError("boolean")
    return chk


def pattern(rx, required=True, what="field"):
    r = re.compile(rx)

    def chk(v):
        if v is None and not required:
            return
        if not isinstance(v, str) or not r.fullmatch(v):
            raise F.FormatError(f"bad {what}")
    return chk


def keyhex(required=True, nullable=False):
    def chk(v):
        if v is None and (nullable or not required):
            return
        if not isinstance(v, str) or not F.HEX64.match(v):
            raise F.FormatError("key")
    return chk


SLUG = pattern(r"[a-z0-9][a-z0-9-]{0,63}", what="slug")
TESTNET_CHAINS = {11155111: "Sepolia", 421614: "Arbitrum Sepolia"}

ROOM_JOIN = TypeSpec("room.join", 1, {"nick": text(24, multiline=False)}, max_refs=0)
WIKI_PAGE = TypeSpec("wiki.page", 1, {"slug": SLUG, "title": text(120, multiline=False)}, 0)
WIKI_REV = TypeSpec("wiki.rev", 1, {"slug": SLUG, "text": text(20000),
                                    "summary": text(200, required=False, multiline=False)}, 1)
WIKI_REVERT = TypeSpec("wiki.revert", 1, {"slug": SLUG,
                                          "summary": text(200, required=False, multiline=False)}, 1)
WIKI_PROTECT = TypeSpec("wiki.protect", 1, {"slug": SLUG, "on": boolean()}, 0)
ID_PROFILE = TypeSpec("id.profile", 1, {"name": text(32, multiline=False),
                                        "bio": text(280, required=False),
                                        "avatar_sha256": hexhash(),
                                        "next_key_hash": hexhash(),
                                        "enc_key": keyhex(required=False)}, max_refs=0, pow_extra=2)
ID_ROTATE = TypeSpec("id.rotate", 1, {"old_key": keyhex(), "new_key": keyhex(nullable=True),
                                      "reason": text(120, required=False, multiline=False)}, 0)
ID_LINK = TypeSpec("id.link", 1, {"other_key": keyhex()}, 0)
# Default currency is SRNET: paid from the buyer's SRNet balance to `vendor_id` (one prepaid
# hour on SRNetBalance, max 20 USDC). ETH/USDC direct (chain_id + pay_to) stays readable as a
# legacy, testnet-only option.
SHOP_LISTING = TypeSpec("shop.listing", 1, {
    "title": text(80, multiline=False), "desc": text(2000, required=False),
    "price": pattern(r"(0|[1-9][0-9]{0,11})(\.[0-9]{1,18})?", what="price"),
    "currency": pattern(r"SRNET|ETH|USDC", what="currency"),
    "vendor_id": pattern(r"0x[0-9a-fA-F]{64}", required=False, what="vendor id"),
    "chain_id": integer(1, 2**31, required=False),
    "pay_to": pattern(r"0x[0-9a-fA-F]{40}", required=False, what="address")}, 0)
SRNET_MAX_PRICE = 20


def shop_listing_error(b: dict) -> str | None:
    if b.get("currency") == "SRNET":
        if not b.get("vendor_id"):
            return "SRNET listings need a vendor_id"
        if b.get("chain_id") or b.get("pay_to"):
            return "SRNET listings take no chain_id/pay_to"
        whole, _, frac = b["price"].partition(".")
        if len(frac) > 6 or float(b["price"]) <= 0 or float(b["price"]) > SRNET_MAX_PRICE:
            return f"SRNET price must be > 0 and at most {SRNET_MAX_PRICE} USDC, 6 decimals max"
        return None
    if not b.get("chain_id") or not b.get("pay_to"):
        return "ETH/USDC listings need chain_id and pay_to"
    return None


# Buyer's receipt after paying from SRNet balance; the seller checks it on-chain (read-only).
SHOP_ORDER = TypeSpec("shop.order", 1, {"lease_id": integer(1, 2**63),
                                        "buyer": pattern(r"0x[0-9a-fA-F]{40}", what="address"),
                                        "note": text(200, required=False)}, 1)
SHOP_WITHDRAW = TypeSpec("shop.withdraw", 1, {"reason": text(200, required=False)}, 1)
SHOP_REVIEW = TypeSpec("shop.review", 1, {"rating": integer(1, 5),
                                          "text": text(500, required=False)}, 1)
def wrap_obj():
    def chk(v):
        if not isinstance(v, dict) or set(v) != {"to", "epk", "ct"}:
            raise F.FormatError("wrap")
        for k, n in (("to", 64), ("epk", 64), ("ct", 96)):
            if not isinstance(v[k], str) or not re.fullmatch(f"[0-9a-f]{{{n}}}", v[k]):
                raise F.FormatError("wrap")
    return chk


def wraps(max_n=16):
    one = wrap_obj()

    def chk(v):
        if v is None:
            return
        if not isinstance(v, list) or len(v) > max_n:
            raise F.FormatError("wraps")
        for w in v:
            one(w)
        if len({w["to"] for w in v}) != len(v):
            raise F.FormatError("duplicate wrap recipient")
    return chk


DROP_MODES = ("plain", "link", "gated")
# drop.item v1, extended: `mode` absent means "plain" (older records keep verifying).
# Encrypted modes: sha256/size describe the ciphertext; name and MIME type are inside it.
# refs[0], if present, is the previous version of this file (same author).
DROP_ITEM = TypeSpec("drop.item", 1, {
    "sha256": hexhash(required=True), "size": integer(1, 300 * 1024 * 1024),  # host policy max_file_bytes applies
    "mime": pattern(r"[a-z]+/[a-z0-9.+-]{1,60}", required=False, what="mime"),
    "name": text(128, required=False, multiline=False), "note": text(500, required=False),
    "mode": pattern(r"plain|link|gated", required=False, what="mode"),
    "wraps": wraps()}, 1)
DROP_GRANT = TypeSpec("drop.grant", 1, {"sha256": hexhash(required=True), "wrap": wrap_obj()}, 1)
DROP_REVOKE = TypeSpec("drop.revoke", 1, {"to": keyhex()}, 1)
DROP_REQUEST = TypeSpec("drop.request", 1, {"enc_key": keyhex(),
                                            "note": text(200, required=False)}, 1)

# Directory: a host operator lists their own collection by signing an entry with that
# collection's root key (the key in the link's #root=). Entries signed by anyone else are ignored.
DIRECTORY_ENTRY = TypeSpec("directory.entry", 1, {
    "link": pattern(r"srnet://[a-z0-9.:\[\]]{1,80}/[a-z]{2,12}/[a-z][a-z0-9_-]{0,31}#root=[0-9a-f]{64}", what="link"),
    "title": text(80, multiline=False), "desc": text(500, required=False)}, 0)

PROFILE_TYPES = (ID_PROFILE, ID_ROTATE, ID_LINK)
KIND_TYPES = {
    "room": TypeSet(ROOM_MSG, ROOM_TOPIC, ROOM_JOIN, *PROFILE_TYPES),
    "wiki": TypeSet(WIKI_PAGE, WIKI_REV, WIKI_REVERT, WIKI_PROTECT, *PROFILE_TYPES),
    "shop": TypeSet(SHOP_LISTING, SHOP_WITHDRAW, SHOP_REVIEW, SHOP_ORDER, *PROFILE_TYPES),
    "drop": TypeSet(DROP_ITEM, DROP_GRANT, DROP_REVOKE, DROP_REQUEST, *PROFILE_TYPES),
    "id": TypeSet(*PROFILE_TYPES),
    "directory": TypeSet(DIRECTORY_ENTRY, *PROFILE_TYPES),
}
ROOM_TYPES = KIND_TYPES["room"]


def codec_for(kind: str):
    return BOARD if kind == "board" else TypedCodec(KIND_TYPES[kind])
