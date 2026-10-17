"""SRNet mail: wire format (addresses, signed + sealed messages, contact tags, inbox auth).

Address card (shared as a srnet://mail link):
  {"ed": <Ed25519 identity hex>, "x": <X25519 hex>, "hosts": [1..3 mail host URLs], "ts": int}
  sig = Ed25519(ed) over canonical("srnet-mail/addr/v1", card).  The link may also carry
  `name=` (a *suggested* petname; unsigned, never trusted).

Message (inner, signed by the sender, then sealed):
  {"v":1,"from","from_x","from_hosts","to","msg_id","ts","subject","body","attach":[links],
   "reply_to"} + "sig" over canonical("srnet-mail/msg/v1", inner).
  Plaintext = u32 len | JSON | zero pad to a power-of-two bucket (1 KiB .. 64 KiB).

Seal ("SRM1"): MAGIC | epk(32) | nonce(12) | ChaCha20-Poly1305(ct+tag)
  key = HKDF-SHA256(X25519(e, recipient_x), salt=MAGIC, info="srnet-mail/seal/v1"+epk+rx)
  AAD = MAGIC + rx + msg_id, so a host can't relabel a blob or move it to another inbox.
  The host sees: recipient, msg_id, padded size, time.  Never the sender or the content.

Contact tag (host-side spam sorting without revealing the sender):
  tag = HMAC-SHA256(X25519(sender_x, recipient_x), "srnet-mail/tag/v1|" + to + "|" + host_id)[:16]
  The recipient uploads the tags of accepted contacts; mail with a matching tag lands in
  "inbox", everything else in the small "requests" pile.  Tags differ per host.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import mbfmt as F
from . import filecrypt as FC

MAGIC = b"SRM1"
ADDR_DOMAIN = "srnet-mail/addr/v1"
MSG_DOMAIN = "srnet-mail/msg/v1"
AUTH_DOMAIN = "srnet-mail/auth/v1"
MIN_BUCKET, MAX_BUCKET = 1024, 64 * 1024
MAX_SEALED = 4 + 32 + 12 + MAX_BUCKET + 16
MAX_SUBJECT, MAX_BODY, MAX_ATTACH = 200, 48 * 1024, 8
MAX_HOSTS = 3
FUTURE_SKEW_S, MAX_AGE_S = 600, 31 * 86400
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MSGID = re.compile(r"^[0-9a-f]{32}$")
TAG_RE = re.compile(r"^[0-9a-f]{32}$")


class MailError(Exception):
    pass


def _x_pub(priv: X25519PrivateKey) -> bytes:
    return bytes.fromhex(FC.pub_hex(priv))


def _sig(key, domain, obj) -> str:
    return key.sign(F.canonical(domain, obj)).hex()


def _ok(pub, sig, domain, obj) -> bool:
    return F._verify(pub, sig, F.canonical(domain, obj))


def check_host_url(h: str) -> str:
    u = urlparse(h)
    if u.scheme not in ("http", "https") or not u.hostname or u.query or u.fragment or len(h) > 200:
        raise MailError(f"bad mail host URL: {h!r}")
    return h.rstrip("/")


# ---- address cards / srnet://mail links -----------------------------------------------
def make_card(key, xkey, hosts, ts=None) -> dict:
    hosts = [check_host_url(h) for h in hosts]
    if not 1 <= len(hosts) <= MAX_HOSTS or len(set(hosts)) != len(hosts):
        raise MailError(f"list 1 to {MAX_HOSTS} distinct mail hosts")
    card = {"ed": F.pub_hex(key), "x": FC.pub_hex(xkey), "hosts": hosts, "ts": int(ts or time.time())}
    return {"card": card, "sig": _sig(key, ADDR_DOMAIN, card)}


def verify_card(env: dict) -> dict:
    c = env.get("card") if isinstance(env, dict) else None
    if not isinstance(c, dict) or set(c) != {"ed", "x", "hosts", "ts"}:
        raise MailError("bad address card")
    if not HEX64.match(str(c["ed"])) or not HEX64.match(str(c["x"])) or not isinstance(c["ts"], int):
        raise MailError("bad address card keys")
    if not isinstance(c["hosts"], list) or not 1 <= len(c["hosts"]) <= MAX_HOSTS:
        raise MailError("bad address card hosts")
    for h in c["hosts"]:
        check_host_url(h)
    if not _ok(c["ed"], env.get("sig"), ADDR_DOMAIN, c):
        raise MailError("address card signature invalid")
    return c


def card_link(env: dict, name: str | None = None) -> str:
    c = env["card"]
    q = [("x", c["x"])] + [("h", h) for h in c["hosts"]] + [("ts", str(c["ts"])), ("sig", env["sig"])]
    if name:
        q.append(("name", name[:64]))
    return f"srnet://mail/{c['ed']}?" + urlencode(q, quote_via=quote)


def parse_link(link: str) -> tuple[dict, str | None]:
    """srnet://mail link -> (verified card env, suggested name or None)."""
    u = urlparse(link.strip())
    if u.scheme != "srnet" or u.netloc != "mail":
        raise MailError("not a srnet://mail link")
    q = parse_qs(u.query)
    try:
        env = {"card": {"ed": u.path.strip("/"), "x": q["x"][0], "hosts": q.get("h", []),
                        "ts": int(q["ts"][0])}, "sig": q["sig"][0]}
    except (KeyError, ValueError, IndexError):
        raise MailError("incomplete srnet://mail link") from None
    verify_card(env)
    name = q.get("name", [None])[0]
    return env, (name[:64] if name else None)


def address_key(link: str) -> str:
    """Stable key of any srnet:// address, for petnames (scheme + kind + path; no query)."""
    u = urlparse(link.strip())
    if u.scheme != "srnet" or not u.netloc:
        raise MailError("not a srnet:// address")
    return f"srnet://{u.netloc}/{u.path.strip('/')}"


# ---- messages -------------------------------------------------------------------------
def _bucket(n):
    b = MIN_BUCKET
    while b < n:
        b <<= 1
    return b


def new_msg_id() -> str:
    return os.urandom(16).hex()


def make_message(key, xkey, my_hosts, to_card: dict, subject: str, body: str, attach=(),
                 reply_to=None, msg_id=None, ts=None) -> dict:
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY or len(attach) > MAX_ATTACH:
        raise MailError("message too long")
    for a in attach:
        FC.parse_link(a)  # attachments are encrypted file-drop links
    inner = {"v": 1, "from": F.pub_hex(key), "from_x": FC.pub_hex(xkey),
             "from_hosts": [check_host_url(h) for h in my_hosts][:MAX_HOSTS],
             "to": to_card["ed"], "msg_id": msg_id or new_msg_id(), "ts": int(ts or time.time()),
             "subject": subject, "body": body, "attach": list(attach), "reply_to": reply_to}
    inner["sig"] = _sig(key, MSG_DOMAIN, inner)
    return inner


def _aad(rx: bytes, msg_id: str) -> bytes:
    return MAGIC + rx + bytes.fromhex(msg_id)


def seal(inner: dict, to_x_hex: str) -> bytes:
    raw = json.dumps(inner, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    plain = struct.pack(">I", len(raw)) + raw
    if len(plain) > MAX_BUCKET:
        raise MailError("message too large after encoding (max 64 KiB)")
    plain += b"\x00" * (_bucket(len(plain)) - len(plain))
    rx = bytes.fromhex(to_x_hex)
    e = X25519PrivateKey.generate()
    epk = _x_pub(e)
    k = HKDF(hashes.SHA256(), 32, salt=MAGIC, info=b"srnet-mail/seal/v1" + epk + rx).derive(
        e.exchange(X25519PublicKey.from_public_bytes(rx)))
    nonce = os.urandom(12)
    return MAGIC + epk + nonce + ChaCha20Poly1305(k).encrypt(nonce, plain, _aad(rx, inner["msg_id"]))


def open_sealed(blob: bytes, xkey: X25519PrivateKey, msg_id: str, me: str, now=None) -> dict:
    """Decrypt + verify. Raises MailError on anything wrong (incl. sender forgery)."""
    if len(blob) < 64 or blob[:4] != MAGIC or len(blob) > MAX_SEALED or not MSGID.match(msg_id):
        raise MailError("not a sealed message")
    epk, nonce, ct = blob[4:36], blob[36:48], blob[48:]
    rx = _x_pub(xkey)
    k = HKDF(hashes.SHA256(), 32, salt=MAGIC, info=b"srnet-mail/seal/v1" + epk + rx).derive(
        xkey.exchange(X25519PublicKey.from_public_bytes(epk)))
    try:
        plain = ChaCha20Poly1305(k).decrypt(nonce, ct, _aad(rx, msg_id))
    except InvalidTag:
        raise MailError("cannot open (not for you, or tampered)") from None
    n = struct.unpack(">I", plain[:4])[0]
    try:
        inner = json.loads(plain[4:4 + n])
    except ValueError:
        raise MailError("bad message encoding") from None
    return verify_message(inner, me, msg_id, now)


def verify_message(inner: dict, me: str, msg_id: str, now=None) -> dict:
    want = {"v", "from", "from_x", "from_hosts", "to", "msg_id", "ts", "subject", "body",
            "attach", "reply_to", "sig"}
    if not isinstance(inner, dict) or set(inner) != want or inner["v"] != 1:
        raise MailError("bad message fields")
    sig = inner["sig"]
    body = {k: v for k, v in inner.items() if k != "sig"}
    if not HEX64.match(str(inner["from"])) or not _ok(inner["from"], sig, MSG_DOMAIN, body):
        raise MailError("sender signature invalid (forged or altered)")
    if inner["to"] != me:
        raise MailError("message is addressed to someone else")
    if inner["msg_id"] != msg_id:
        raise MailError("message id mismatch")
    now = now or time.time()
    if not isinstance(inner["ts"], int) or inner["ts"] > now + FUTURE_SKEW_S or inner["ts"] < now - MAX_AGE_S:
        raise MailError("message timestamp out of range")
    if not isinstance(inner["subject"], str) or not isinstance(inner["body"], str) or \
            not isinstance(inner["attach"], list) or len(inner["attach"]) > MAX_ATTACH:
        raise MailError("bad message content")
    return inner


# ---- contact tags + inbox auth --------------------------------------------------------
def contact_tag(my_xkey: X25519PrivateKey, other_x_hex: str, recipient_ed: str, host_id: str) -> str:
    shared = my_xkey.exchange(X25519PublicKey.from_public_bytes(bytes.fromhex(other_x_hex)))
    msg = f"srnet-mail/tag/v1|{recipient_ed}|{host_id}".encode()
    return hmac.new(shared, msg, hashlib.sha256).hexdigest()[:32]


def args_hash(args: dict) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def auth_obj(host_id, to, op, nonce, args) -> dict:
    return {"host": host_id, "to": to, "op": op, "nonce": nonce, "args": args_hash(args)}


def sign_auth(key, host_id, op, nonce, args) -> dict:
    to = F.pub_hex(key)
    return {"to": to, "op": op, "nonce": nonce, "args": args,
            "sig": _sig(key, AUTH_DOMAIN, auth_obj(host_id, to, op, nonce, args))}


def verify_auth(req: dict, host_id: str) -> bool:
    try:
        return _ok(req["to"], req["sig"], AUTH_DOMAIN,
                   auth_obj(host_id, req["to"], req["op"], req["nonce"], req.get("args", {})))
    except (KeyError, TypeError):
        return False


def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def b64d(s: str) -> bytes:
    return base64.b64decode(s, validate=True)
