"""Optional add-ons for the SRNet client.

An add-on is a Python package that declares an entry point in the "srnet.addons" group, pointing at
an object with:

  name, title, version
  register(subparsers) -> {command: fn(args, state)}   extra `srnet` commands
  doctor(state, line)                                  extra `srnet doctor` checks
  start_ui_backend(state) -> "http://127.0.0.1:<port>/#<token>"   backend for its app tab
  ui_manifest() -> {"name", "title", "tab": bool, "slow": [calls that may take minutes]}

The SRNet core itself has no exit, wallet or payment code. The exits add-on is "srnet-exits"
(the SRNet-VPSHost-ExitPack repo); without it, `srnet exits` and friends print how to install it.

For a checkout without `pip install`, SRNET_ADDON_MODULES="module:ATTR,..." loads add-ons by import.
"""
from __future__ import annotations

import importlib
import os
import sys

GROUP = "srnet.addons"
EXITS_COMMANDS = ("exits", "buy", "paid", "connect", "status", "disconnect", "verify")
EXITS_SOURCE = "git+https://github.com/SRNet-lab/SRNet-VPSHost-ExitPack"
EXITS_HINT = ("Exits are an add-on. Install it with ./install.sh --with-exits in your SRNet-Desktop checkout, "
              f'or: "{sys.executable}" -m pip install "srnet-exits @ {EXITS_SOURCE}"')

_cache: dict | None = None


def _from_entry_points() -> list:
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return []
    try:
        eps = entry_points(group=GROUP)
    except TypeError:  # Python 3.9 style
        eps = entry_points().get(GROUP, [])
    return list(eps)


def load(refresh: bool = False) -> dict:
    """{name: addon}. An add-on that fails to load is skipped with a note on stderr."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    found: dict = {}
    if os.environ.get("SRNET_NO_ADDONS") == "1":
        _cache = found
        return found
    for ep in _from_entry_points():
        try:
            a = ep.load()
            found.setdefault(getattr(a, "name", ep.name), a)
        except Exception as e:
            print(f"note: add-on {ep.name} did not load: {e}", file=sys.stderr)
    for spec in filter(None, (os.environ.get("SRNET_ADDON_MODULES") or "").split(",")):
        mod, _, attr = spec.strip().partition(":")
        try:
            a = getattr(importlib.import_module(mod), attr or "ADDON")
            found.setdefault(a.name, a)
        except Exception as e:
            print(f"note: add-on {spec} did not load: {e}", file=sys.stderr)
    _cache = found
    return found


def ui_env(addons: dict, st) -> str:
    """Start each add-on's UI backend; returns the SRNET_ADDONS value for the SNApps core."""
    import json
    out = []
    for name, a in sorted(addons.items()):
        try:
            m = a.ui_manifest()
            if not m.get("tab"):
                continue
            url = a.start_ui_backend(st)
            out.append({"name": m.get("name", name), "title": m.get("title", name), "url": url,
                        "slow": list(m.get("slow") or [])})
        except Exception as e:
            print(f"note: add-on {name} UI did not start: {e}", file=sys.stderr)
    return json.dumps(out)
