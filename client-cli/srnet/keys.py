"""Buyer wallet key: encrypted keystore (default) or plaintext flag/env/config (warned).

The keystore is a standard Ethereum V3 JSON file (scrypt), the same format geth,
MetaMask exports and Foundry use. Default path: $SRNET_HOME/keystore.json.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

PASSWORD_ENV = "SRNET_KEYSTORE_PASSWORD"


class KeyError_(Exception):
    pass


def default_path(root: Path) -> Path:
    return root / "keystore.json"


def _password(confirm: bool = False) -> str:
    pw = os.environ.get(PASSWORD_ENV)
    if pw is not None:
        return pw
    if not sys.stdin.isatty():
        raise KeyError_(f"keystore is locked: set {PASSWORD_ENV} or run from a terminal")
    pw = getpass.getpass("keystore passphrase: ")
    if confirm:
        if not pw:
            raise KeyError_("empty passphrase")
        if getpass.getpass("again: ") != pw:
            raise KeyError_("passphrases don't match")
    return pw


def write(path: Path, private_key: str, password: str, *, iterations: int | None = None) -> str:
    """Encrypt private_key into a V3 keystore at path (mode 600). Returns the address."""
    from eth_account import Account
    path = Path(path)
    if path.exists():
        raise KeyError_(f"{path} already exists; move it away first")
    kw = {"iterations": iterations} if iterations else {}
    data = Account.encrypt(private_key, password, kdf="scrypt", **kw)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return Account.from_key(private_key).address


def new(path: Path, password: str, **kw) -> str:
    from eth_account import Account
    return write(path, Account.create().key.hex(), password, **kw)


def unlock(path: Path, password: str) -> str:
    """Decrypt a keystore; returns 0x-hex private key."""
    from eth_account import Account
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise KeyError_(f"no keystore at {path}; run `srnet key new` or `srnet key import`") from None
    except json.JSONDecodeError:
        raise KeyError_(f"{path} is not a keystore JSON file") from None
    try:
        return "0x" + bytes(Account.decrypt(data, password)).hex()
    except ValueError:
        raise KeyError_("wrong keystore passphrase") from None


def address_of(path: Path) -> str | None:
    try:
        a = json.loads(Path(path).read_text()).get("address")
        return "0x" + a.removeprefix("0x") if a else None
    except Exception:
        return None


def resolve(args, cfg: dict, root: Path, *, warn: bool = True) -> tuple[str | None, str]:
    """(private_key or None, source description). Plaintext sources win if set, with a warning."""
    for val, src in ((getattr(args, "private_key", None), "--private-key flag"),
                     (cfg.get("private_key"), "config.json private_key"),
                     (os.environ.get("SRNET_PRIVATE_KEY"), "SRNET_PRIVATE_KEY env")):
        if val:
            if warn:
                print(f"warning: using a plaintext key from {src}; prefer `srnet key import` (encrypted keystore)",
                      file=sys.stderr)
            return val, f"{src} (plaintext)"
    ks = Path(getattr(args, "keystore", None) or cfg.get("keystore") or default_path(root))
    if not ks.exists():
        return None, f"none (no keystore at {ks})"
    return unlock(ks, _password()), f"keystore {ks}"
