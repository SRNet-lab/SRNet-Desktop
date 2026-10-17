"""Signed message-board format, v1.  See docs/PROTOCOL.md.

All signed objects use domain-separated canonical JSON:
    DOMAIN + "\n" + json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
encoded UTF-8.  Domains are unique to this system so signatures never cross over to the
exit board (or between object types).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

__all__ = [
    "POST_DOMAIN", "CHECKPOINT_DOMAIN", "DELEGATION_DOMAIN", "POW_DOMAIN", "CHAIN_DOMAIN",
    "MAX_BODY_CHARS", "MAX_POST_BYTES", "CLOCK_SKEW_S", "MAX_DELEGATION_S", "HEX64", "_verify", "FormatError",
    "canonical", "zb32", "new_key", "key_from_seed", "pub_hex", "load_pub",
    "make_post", "sign_post", "post_id", "verify_post", "pow_ok", "solve_pow",
    "chain_next", "GENESIS", "make_delegation", "verify_delegation",
    "make_checkpoint", "verify_checkpoint", "CheckpointPin", "MOD_DOMAIN", "DELETE_DOMAIN",
    "record_id", "make_mod", "verify_mod", "make_delete", "verify_delete", "policy_hash",
]

POST_DOMAIN = "srnet-msgboard-post/v1"
CHECKPOINT_DOMAIN = "srnet-msgboard-checkpoint/v1"
DELEGATION_DOMAIN = "srnet-msgboard-delegation/v1"
POW_DOMAIN = "srnet-msgboard-pow/v1"
CHAIN_DOMAIN = "srnet-msgboard-chain/v1"

MAX_BODY_CHARS = 500
MAX_POST_BYTES = 4096
CLOCK_SKEW_S = 600
MAX_DELEGATION_S = 90 * 86400  # host-key authorisations last at most 90 days  # posts more than 10 min in the future, or older than this at submit, are refused
BOARD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
POST_FIELDS = {"v", "board", "author", "ts", "body", "reply_to", "pow"}

GENESIS = "0" * 64


class FormatError(Exception):
    pass


def canonical(domain: str, obj: dict) -> bytes:
    return (domain + "\n" + json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False)).encode("utf-8")


_ZB = "ybndrfg8ejkmcpqxot1uwisza345h769"


def zb32(data: bytes) -> str:
    bits = int.from_bytes(data, "big")
    n = len(data) * 8
    out = []
    for i in range(0, n, 5):
        shift = n - i - 5
        v = (bits >> shift) & 31 if shift >= 0 else (bits << -shift) & 31
        out.append(_ZB[v])
    return "".join(out)


# ---- keys -------------------------------------------------------------------------------
def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def key_from_seed(seed: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def pub_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw).hex()


def load_pub(h: str) -> Ed25519PublicKey:
    if not isinstance(h, str) or not HEX64.match(h):
        raise FormatError("bad public key")
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(h))


def _verify(pub_h: str, sig_h: str, msg: bytes) -> bool:
    try:
        if not isinstance(sig_h, str) or not re.fullmatch(r"[0-9a-f]{128}", sig_h):
            return False
        load_pub(pub_h).verify(bytes.fromhex(sig_h), msg)
        return True
    except (InvalidSignature, FormatError, ValueError):
        return False


# ---- proof of work ------------------------------------------------------------------------
def _pow_digest(post: dict) -> bytes:
    """PoW covers the whole unsigned post including pow.bits and pow.salt."""
    return hashlib.sha256(canonical(POW_DOMAIN, post)).digest()


def _lead_zero_bits(d: bytes) -> int:
    n = 0
    for b in d:
        if b == 0:
            n += 8
            continue
        return n + (8 - b.bit_length())
    return n


def pow_ok(post: dict, required_bits: int) -> bool:
    p = post.get("pow")
    if required_bits <= 0:
        return True
    if not isinstance(p, dict) or not isinstance(p.get("bits"), int) or p["bits"] < required_bits:
        return False
    return _lead_zero_bits(_pow_digest(post)) >= p["bits"]


def solve_pow(post: dict, bits: int, prefix: str | None = None) -> dict:
    post = dict(post)
    i = 0
    prefix = os.urandom(4).hex() if prefix is None else prefix
    while True:
        post["pow"] = {"bits": bits, "salt": f"{prefix}{i:x}"}
        if _lead_zero_bits(_pow_digest(post)) >= bits:
            return post
        i += 1


# ---- posts ----------------------------------------------------------------------------------
def make_post(key: Ed25519PrivateKey, board: str, body: str, reply_to: str | None = None,
              ts: int | None = None, pow_bits: int = 0) -> dict:
    post = {"v": 1, "board": board, "author": pub_hex(key),
            "ts": int(time.time()) if ts is None else ts,
            "body": unicodedata.normalize("NFC", body), "reply_to": reply_to,
            "pow": {"bits": 0, "salt": ""}}
    _check_shape(post)
    if pow_bits:
        post = solve_pow(post, pow_bits)
    return sign_post(key, post)


def sign_post(key: Ed25519PrivateKey, post: dict) -> dict:
    post = {k: v for k, v in post.items() if k not in ("sig", "id")}
    sig = key.sign(canonical(POST_DOMAIN, post)).hex()
    return {"post": post, "sig": sig, "id": post_id(post)}


def post_id(post: dict) -> str:
    """Content address: first 16 bytes of SHA-256 over the canonical signed bytes, z-base32."""
    return zb32(hashlib.sha256(canonical(POST_DOMAIN, post)).digest()[:16])


def _check_shape(post: dict) -> None:
    if not isinstance(post, dict) or set(post) != POST_FIELDS:
        raise FormatError("post fields")
    if post["v"] != 1:
        raise FormatError("version")
    if not isinstance(post["board"], str) or not BOARD_RE.match(post["board"]):
        raise FormatError("board")
    if not isinstance(post["author"], str) or not HEX64.match(post["author"]):
        raise FormatError("author")
    if not isinstance(post["ts"], int) or isinstance(post["ts"], bool) or post["ts"] < 0:
        raise FormatError("ts")
    b = post["body"]
    if not isinstance(b, str) or not b.strip() or len(b) > MAX_BODY_CHARS:
        raise FormatError("body length")
    if unicodedata.normalize("NFC", b) != b:
        raise FormatError("body not NFC")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in b):
        raise FormatError("control characters")
    r = post["reply_to"]
    if r is not None and (not isinstance(r, str) or not re.fullmatch(f"[{_ZB}]{{26}}", r)):
        raise FormatError("reply_to")
    p = post["pow"]
    if not isinstance(p, dict) or set(p) != {"bits", "salt"} or not isinstance(p["bits"], int) \
            or not isinstance(p["salt"], str) or len(p["salt"]) > 64:
        raise FormatError("pow")


def verify_post(env: dict, board: str | None = None, now: float | None = None,
                pow_bits: int = 0, check_time: bool = False) -> str:
    """Verify a post envelope {post, sig[, id]}.  Returns the post id or raises FormatError."""
    if not isinstance(env, dict) or not isinstance(env.get("post"), dict):
        raise FormatError("envelope")
    post = env["post"]
    _check_shape(post)
    if len(canonical(POST_DOMAIN, post)) > MAX_POST_BYTES:
        raise FormatError("too large")
    if board is not None and post["board"] != board:
        raise FormatError("wrong board")
    if not _verify(post["author"], env.get("sig"), canonical(POST_DOMAIN, post)):
        raise FormatError("bad signature")
    pid = post_id(post)
    if "id" in env and env["id"] != pid:
        raise FormatError("id mismatch")
    if check_time:
        now = time.time() if now is None else now
        if post["ts"] > now + 60 or post["ts"] < now - CLOCK_SKEW_S:
            raise FormatError("stale or future timestamp")
    if not pow_ok(post, pow_bits):
        raise FormatError("insufficient proof of work")
    return pid


# ---- hash chain -------------------------------------------------------------------------
def chain_next(prev_head: str, seq: int, pid: str) -> str:
    return hashlib.sha256(f"{CHAIN_DOMAIN}\n{prev_head}\n{seq}\n{pid}".encode()).hexdigest()


# ---- delegation: offline board root key -> online host key -------------------------------
def make_delegation(root: Ed25519PrivateKey, board: str, host_pub: str, expires: int,
                    policy_hash: str = "") -> dict:
    d = {"v": 1, "board": board, "root": pub_hex(root), "host": host_pub,
         "issued": int(time.time()), "expires": expires, "policy": policy_hash}
    return {"delegation": d, "sig": root.sign(canonical(DELEGATION_DOMAIN, d)).hex()}


def verify_delegation(env: dict, root_pin: str, board: str, now: float | None = None) -> str:
    d = env.get("delegation") if isinstance(env, dict) else None
    if not isinstance(d, dict) or d.get("root") != root_pin or d.get("board") != board:
        raise FormatError("delegation not for this root/board")
    if not _verify(root_pin, env.get("sig"), canonical(DELEGATION_DOMAIN, d)):
        raise FormatError("bad delegation signature")
    if (time.time() if now is None else now) > d.get("expires", 0):
        raise FormatError("delegation expired")
    if d.get("expires", 0) - d.get("issued", 0) > MAX_DELEGATION_S + 3600:
        raise FormatError("delegation longer than 90 days")
    load_pub(d["host"])
    return d["host"]


# ---- checkpoints ------------------------------------------------------------------------
def make_checkpoint(host: Ed25519PrivateKey, board: str, seq: int, head: str,
                    valid_s: int = 3 * 3600, now: int | None = None, policy: str = "",
                    rseq: int = 0) -> dict:
    """policy = policy_hash of the served policy; rseq = number of records (mod/delete)."""
    now = int(time.time()) if now is None else now
    c = {"v": 1, "board": board, "host": pub_hex(host), "seq": seq, "head": head,
         "issued": now, "expires": now + valid_s, "policy": policy, "rseq": rseq}
    return {"checkpoint": c, "sig": host.sign(canonical(CHECKPOINT_DOMAIN, c)).hex()}


def verify_checkpoint(env: dict, host_pub: str, board: str, now: float | None = None) -> dict:
    c = env.get("checkpoint") if isinstance(env, dict) else None
    if not isinstance(c, dict) or c.get("host") != host_pub or c.get("board") != board:
        raise FormatError("checkpoint not from delegated host")
    if not _verify(host_pub, env.get("sig"), canonical(CHECKPOINT_DOMAIN, c)):
        raise FormatError("bad checkpoint signature")
    now = time.time() if now is None else now
    if c["issued"] > now + 300:
        raise FormatError("checkpoint from the future")
    if now > c["expires"]:
        raise FormatError("checkpoint expired")
    if not isinstance(c["seq"], int) or not HEX64.match(c.get("head", "")):
        raise FormatError("checkpoint fields")
    return c


class CheckpointPin:
    """Rollback / equivocation memory (like the exit client's board-cache.json)."""

    def __init__(self, seq: int = 0, head: str = GENESIS):
        self.seq, self.head = seq, head

    def check(self, c: dict) -> None:
        if c["seq"] < self.seq:
            raise FormatError("rollback: checkpoint seq went backwards")
        if c["seq"] == self.seq and c["head"] != self.head and self.seq != 0:
            raise FormatError("equivocation: same seq, different head")


# ---- moderation and deletion records ------------------------------------------------------
MOD_DOMAIN = "srnet-msgboard-mod/v1"
DELETE_DOMAIN = "srnet-msgboard-delete/v1"
POLICY_DOMAIN = "srnet-msgboard-policy/v1"
MOD_ACTIONS = {"hide", "unhide", "remove", "pin", "unpin"}  # remove/pin: posts only
MOD_TARGETS = {"post", "author"}
MAX_DELETE_TARGETS = 50
_ID_RE = re.compile(f"^[{_ZB}]{{26}}$")


def record_id(domain: str, obj: dict) -> str:
    return hashlib.sha256(canonical(domain, obj)).hexdigest()[:32]


def make_mod(host: Ed25519PrivateKey, board: str, action: str, target_type: str, target: str,
             reason: str, ts: int | None = None) -> dict:
    r = {"v": 1, "board": board, "host": pub_hex(host), "action": action,
         "target_type": target_type, "target": target, "reason": reason,
         "ts": int(time.time()) if ts is None else ts}
    _check_mod(r)
    return {"mod": r, "sig": host.sign(canonical(MOD_DOMAIN, r)).hex()}


def _check_mod(r):
    if not isinstance(r, dict) or set(r) != {"v", "board", "host", "action", "target_type",
                                             "target", "reason", "ts"}:
        raise FormatError("mod fields")
    if r["action"] not in MOD_ACTIONS or r["target_type"] not in MOD_TARGETS:
        raise FormatError("mod action/target_type")
    if r["action"] in ("remove", "pin", "unpin") and r["target_type"] != "post":
        raise FormatError(f"{r['action']} targets a post")
    ok = _ID_RE.match(r["target"]) if r["target_type"] == "post" else HEX64.match(r["target"])
    if not isinstance(r["target"], str) or not ok:
        raise FormatError("mod target")
    if not isinstance(r["reason"], str) or len(r["reason"]) > 200:
        raise FormatError("mod reason")


def verify_mod(env: dict, host_pub: str, board: str) -> dict:
    r = env.get("mod") if isinstance(env, dict) else None
    _check_mod(r)
    if r["host"] != host_pub or r["board"] != board:
        raise FormatError("mod record not from this board's host")
    if not _verify(host_pub, env.get("sig"), canonical(MOD_DOMAIN, r)):
        raise FormatError("bad mod signature")
    return r


def make_delete(author: Ed25519PrivateKey, board: str, targets: list[str],
                ts: int | None = None) -> dict:
    r = {"v": 1, "board": board, "author": pub_hex(author), "targets": sorted(set(targets)),
         "ts": int(time.time()) if ts is None else ts}
    _check_delete(r)
    return {"delete": r, "sig": author.sign(canonical(DELETE_DOMAIN, r)).hex()}


def _check_delete(r):
    if not isinstance(r, dict) or set(r) != {"v", "board", "author", "targets", "ts"}:
        raise FormatError("delete fields")
    t = r["targets"]
    if not isinstance(t, list) or not 1 <= len(t) <= MAX_DELETE_TARGETS or \
            not all(isinstance(x, str) and _ID_RE.match(x) for x in t):
        raise FormatError("delete targets")
    if not isinstance(r["author"], str) or not HEX64.match(r["author"]):
        raise FormatError("delete author")


def verify_delete(env: dict, board: str) -> dict:
    r = env.get("delete") if isinstance(env, dict) else None
    _check_delete(r)
    if r["board"] != board:
        raise FormatError("delete for another board")
    if not _verify(r["author"], env.get("sig"), canonical(DELETE_DOMAIN, r)):
        raise FormatError("bad delete signature")
    return r


def policy_hash(policy: dict) -> str:
    return hashlib.sha256(canonical(POLICY_DOMAIN, policy)).hexdigest()
