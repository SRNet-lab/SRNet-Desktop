"""Client-side file encryption for drops (format "srf1").

Plaintext stream = u32 meta_len | meta JSON {"name","mime","size"} | file bytes | zero padding,
padded to a size bucket so the host learns only the bucket, not the exact size or name.
Encryption: ChaCha20-Poly1305, one random 32-byte key per file (per version), 64 KiB chunks.
Nonce per chunk = 11-byte big-endian chunk counter + 1 byte (1 on the last chunk, else 0)
(the STREAM construction), so chunks can't be reordered, dropped or truncated. The 8-byte
header is authenticated as AAD on every chunk.

Key wrapping (key-gated access): X25519 to the recipient's encryption key. The client derives
its X25519 key from the author seed with HKDF, so a profile's `enc_key` is linked to its
Ed25519 author key. wrap = ephemeral X25519 + HKDF-SHA256 -> ChaCha20-Poly1305 over the file
key, AAD = ciphertext sha256 + recipient key, so a wrap can't be moved to another file.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"SRF1"
CHUNK = 64 * 1024
TAG = 16
MAX_PLAIN = 256 * 1024 * 1024        # protocol ceiling; each host sets its own max_file_bytes
DEFAULT_MAX_FILE = 25 * 1024 * 1024  # host policy default (max_file_bytes)
LEGACY_MAX_FILE = 4 * 1024 * 1024    # hosts that advertise no limit (older host packs)
MIN_BUCKET = 4096
WRAP_INFO = b"srnet-typed/file-wrap/v1"
XKEY_INFO = b"srnet-typed/x25519/v1"


class CryptoError(Exception):
    pass


def bucket(n: int) -> int:
    """Smallest power of two >= n (at least 4 KiB)."""
    b = MIN_BUCKET
    while b < n:
        b <<= 1
    return b


def _header(padded_len: int) -> bytes:
    return MAGIC + struct.pack(">I", padded_len)


def _nonce(i: int, last: bool) -> bytes:
    return i.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def max_cipher_len(max_plain: int | None = None) -> int:
    """Largest ciphertext for a file of at most max_plain bytes (default: protocol ceiling)."""
    p = bucket((MAX_PLAIN if max_plain is None else max_plain) + 1024)
    return 8 + p + (p // CHUNK + 1) * TAG


def encrypt(data: bytes, name: str, mime: str, key: bytes | None = None,
            max_plain: int | None = None) -> tuple[bytes, bytes]:
    """Returns (ciphertext blob, 32-byte file key)."""
    lim = MAX_PLAIN if max_plain is None else min(max_plain, MAX_PLAIN)
    if len(data) > lim:
        raise CryptoError(f"file too large (max {lim} bytes)")
    key = key or os.urandom(32)
    meta = json.dumps({"name": name, "mime": mime, "size": len(data)}, separators=(",", ":")).encode()
    plain = struct.pack(">I", len(meta)) + meta + data
    padded = bucket(len(plain))
    plain += b"\x00" * (padded - len(plain))
    hdr = _header(padded)
    aead = ChaCha20Poly1305(key)
    n = max(1, -(-padded // CHUNK))
    out = [hdr]
    for i in range(n):
        out.append(aead.encrypt(_nonce(i, i == n - 1), plain[i * CHUNK:(i + 1) * CHUNK], hdr))
    return b"".join(out), key


def decrypt(blob: bytes, key: bytes) -> tuple[bytes, dict]:
    """Returns (file bytes, meta). Raises CryptoError on a wrong key or any tampering."""
    if len(blob) < 8 or blob[:4] != MAGIC or len(key) != 32:
        raise CryptoError("not an encrypted file")
    padded = struct.unpack(">I", blob[4:8])[0]
    hdr = blob[:8]
    n = max(1, -(-padded // CHUNK))
    body = blob[8:]
    if len(body) != padded + n * TAG:
        raise CryptoError("truncated or extended ciphertext")
    aead = ChaCha20Poly1305(key)
    parts, off = [], 0
    try:
        for i in range(n):
            ln = min(CHUNK, padded - i * CHUNK) + TAG
            parts.append(aead.decrypt(_nonce(i, i == n - 1), body[off:off + ln], hdr))
            off += ln
    except InvalidTag:
        raise CryptoError("wrong key or tampered file") from None
    plain = b"".join(parts)
    ml = struct.unpack(">I", plain[:4])[0]
    meta = json.loads(plain[4:4 + ml])
    data = plain[4 + ml:4 + ml + int(meta["size"])]
    return data, meta


# ---- keys and wrapping -----------------------------------------------------------------
def x25519_from_seed(seed: bytes) -> X25519PrivateKey:
    k = HKDF(hashes.SHA256(), 32, salt=None, info=XKEY_INFO).derive(seed)
    return X25519PrivateKey.from_private_bytes(k)


def pub_hex(priv: X25519PrivateKey) -> str:
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _kek(shared: bytes, epk: bytes, rpk: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, salt=None, info=WRAP_INFO + epk + rpk).derive(shared)


def wrap(file_key: bytes, to_hex: str, blob_sha: str) -> dict:
    rpk = bytes.fromhex(to_hex)
    e = X25519PrivateKey.generate()
    epk = bytes.fromhex(pub_hex(e))
    kek = _kek(e.exchange(X25519PublicKey.from_public_bytes(rpk)), epk, rpk)
    ct = ChaCha20Poly1305(kek).encrypt(b"\x00" * 12, file_key, bytes.fromhex(blob_sha) + rpk)
    return {"to": to_hex, "epk": epk.hex(), "ct": ct.hex()}


def unwrap(w: dict, priv: X25519PrivateKey, blob_sha: str) -> bytes:
    rpk = bytes.fromhex(pub_hex(priv))
    if w.get("to") != rpk.hex():
        raise CryptoError("wrap is for someone else")
    epk = bytes.fromhex(w["epk"])
    kek = _kek(priv.exchange(X25519PublicKey.from_public_bytes(epk)), epk, rpk)
    try:
        return ChaCha20Poly1305(kek).decrypt(b"\x00" * 12, bytes.fromhex(w["ct"]), bytes.fromhex(blob_sha) + rpk)
    except InvalidTag:
        raise CryptoError("bad key wrap") from None


# ---- public links: key in the URL fragment (never sent to the host) --------------------
def link(base_url: str, blob_sha: str, key: bytes) -> str:
    k = base64.urlsafe_b64encode(key).rstrip(b"=").decode()
    return f"{base_url.rstrip('/')}/v1/blob/{blob_sha}#k={k}"


def parse_link(url: str) -> tuple[str, str, bytes]:
    """(fetch URL without fragment, blob sha256, key)."""
    base, _, frag = url.partition("#")
    if not frag.startswith("k="):
        raise CryptoError("link has no key")
    sha = base.rstrip("/").rsplit("/", 1)[-1]
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise CryptoError("link has no file hash")
    k = frag[2:]
    key = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4))
    if len(key) != 32:
        raise CryptoError("bad key in link")
    return base, sha, key


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
