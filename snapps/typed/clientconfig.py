"""Client config: the shipped defaults (default-client.json) with an optional user config on top.

Layering rules
- collections merge by id: a user entry with the same id replaces the shipped one,
  {"id": X, "disabled": true} drops it, new ids are added after the shipped ones.
- mail: user keys override shipped keys; a user "hosts" or "contacts" list replaces the shipped list.
- every other top-level key: the user value wins.
Shipped entries that still carry a REPLACE-ME placeholder, or a root_pin that isn't 64 hex,
are skipped, so an unconfigured build starts with an empty sidebar instead of errors.
A pre-joined collection the user left stays left: Core records it in prefs["left"].
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULTS = Path(__file__).with_name("default-client.json")
PLACEHOLDER = "REPLACE-ME"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _has_placeholder(v) -> bool:
    if isinstance(v, str):
        return PLACEHOLDER in v
    if isinstance(v, dict):
        return any(_has_placeholder(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_placeholder(x) for x in v)
    return False


def _usable(c: dict) -> bool:
    if not isinstance(c, dict) or c.get("disabled") or _has_placeholder(c):
        return False
    if not (c.get("id") or c.get("name")) or not c.get("kind"):
        return False
    return "root_pin" not in c or bool(HEX64.match(str(c["root_pin"])))


def merge(defaults: dict, user: dict | None) -> dict:
    user = user or {}
    out = {k: v for k, v in defaults.items() if not k.startswith("_")}
    out.update({k: v for k, v in user.items() if k not in ("collections", "mail") and not k.startswith("_")})
    colls: dict[str, dict] = {}
    for c in list(defaults.get("collections", [])) + list(user.get("collections", [])):
        if isinstance(c, dict) and (c.get("id") or c.get("name")):
            colls[c.get("id") or c["name"]] = c
    out["collections"] = [c for c in colls.values() if _usable(c)]
    mail = dict(defaults.get("mail") or {}, **(user.get("mail") or {}))
    mail["hosts"] = [h for h in mail.get("hosts", []) if isinstance(h, str) and not _has_placeholder(h)]
    if "contacts" in mail:
        mail["contacts"] = [c for c in mail["contacts"] if isinstance(c, dict)
                            and isinstance(c.get("link"), str) and not c.get("disabled")
                            and not _has_placeholder(c)]
    out["mail"] = mail
    return out


def load(user_path=None, defaults_path=None) -> dict:
    dp = Path(defaults_path) if defaults_path else DEFAULTS
    defaults = json.loads(dp.read_text(encoding="utf-8")) if dp.exists() else {}
    user = json.loads(Path(user_path).read_text(encoding="utf-8")) if user_path else None
    return merge(defaults, user)
