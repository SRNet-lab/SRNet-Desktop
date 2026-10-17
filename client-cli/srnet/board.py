"""Fetch and verify the signed exit board.

The board is served on three .sesh names, tried in order. Each copy must be signed by the
pinned board key; a copy that fails is skipped. Bad or stale listings are dropped. If no copy
verifies, the last verified board in the cache is used while it is still unexpired.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_V = Path(__file__).resolve().parents[1] / "vendor"
if not _V.is_dir():
    _V = Path(__file__).resolve().parent / "vendor"
if str(_V) not in sys.path:
    sys.path.insert(0, str(_V))
import boardfmt as B  # noqa: E402
import seshdns  # noqa: E402

BOARD_PIN = "qbhfd6kc4qijpdpjkd8nfcq55r377m1ff3o6ontfyikygsycywfo.sesh"
BOARD_NAMES = (
    "exitsrjnc83har7a17mdtjpgi1j3odgiphaffe9ejfq4wnbdxkio.sesh",
    "exitstaf5qs8so5g4ykzodhiyh79zjy3gqmszyzyhhm6r4szpwco.sesh",
    "exitsdr3hzeqcwon7mqa7z87h8rn1j786uabdq5wzjw11us1nroo.sesh",
)
TIMEOUT_S = 30


def resolve(board: str) -> str:
    """Base URL for a board given as a bare .sesh name or URL."""
    if board.endswith(".sesh") and "/" not in board:
        return f"http://{board}/"
    u = urllib.parse.urlparse(board)
    host = u.hostname or ""
    if u.scheme == "https" or (u.scheme == "http" and (host in ("127.0.0.1", "::1", "localhost")
                                                        or host.endswith(".sesh"))):
        return board if board.endswith("/") else board + "/"
    raise ValueError("board must be a .sesh name, http://<name>.sesh/, a loopback URL, or https://")


def sources(cfg: dict) -> list[str]:
    s = cfg.get("board_urls") or list(BOARD_NAMES)
    return [s] if isinstance(s, str) else list(s)


def pin_of(cfg: dict) -> str:
    return cfg.get("board_pin") or BOARD_PIN


def _get(url: str, dns: str | None = None) -> bytes:
    """System resolver first; for .sesh names fall back to the router's DNS listener."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:  # noqa: S310 (scheme checked in resolve)
            data = r.read(B.MAX_BOARD_BYTES + 1)
    except OSError:
        if not (urllib.parse.urlparse(url).hostname or "").endswith(".sesh"):
            raise
        _, data = seshdns.open_url(url, timeout=TIMEOUT_S, dns=dns, limit=B.MAX_BOARD_BYTES)
    if len(data) > B.MAX_BOARD_BYTES:
        raise B.BoardError("board too large")
    return data


def fetch(urls: list[str], pin: str, cache_dir: Path, now: float | None = None,
          dns: str | None = None) -> tuple[list[dict], dict]:
    """Returns (listings, info). info: seq, issued, source (url or 'cache'), dropped, errors."""
    cache = cache_dir / "board-cache.json"
    seen = None
    if cache.exists():
        try:
            seen = json.loads(cache.read_text())
        except ValueError:
            seen = None
    if seen and seen.get("pin") != pin:
        seen = None
    errors = []
    for u in urls:
        try:
            base = resolve(u)
            body_b = _get(base + "board.json", dns)
            sig = _get(base + "board.sig", dns).decode()
            body, good, dropped = B.verify_board(body_b, sig, pin, now=now, seen=seen)
        except (B.BoardError, OSError, ValueError) as e:
            errors.append(f"{u}: {e}")
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pin": pin, "seq": body["seq"], "hash": B.body_hash(body),
                                   "body": body_b.decode(), "sig": sig.strip()}))
        tmp.replace(cache)
        return good, {"seq": body["seq"], "issued": body["issued"], "source": u,
                      "dropped": dropped, "errors": errors}
    if seen:
        try:
            body, good, dropped = B.verify_board(seen["body"].encode(), seen["sig"], pin, now=now)
            return good, {"seq": body["seq"], "issued": body["issued"], "source": "cache",
                          "dropped": dropped, "errors": errors}
        except B.BoardError as e:
            errors.append(f"cache: {e}")
    raise B.BoardError("no board copy verified: " + "; ".join(errors))


def table(listings: list[dict], now: float) -> str:
    from .listings import exit_dns
    rows = [("SELLER", "REGION", "USDC/HR", "EXIT DNS", "LAST REFRESH")]
    for d in sorted(listings, key=lambda d: d["listing"]["seller"]):
        b = d["listing"]
        age = max(0, int(now - int(b.get("issued", 0))))
        ago = f"{age // 60}m ago" if age < 3600 else f"{age // 3600}h{(age % 3600) // 60:02d}m ago"
        rows.append((b["seller"], b.get("region") or "-",
                     str(b.get("price_per_hour_usdc", b.get("price_usdc", "?"))),
                     "yes" if exit_dns(b) else "NONE", ago))
    w = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in rows)


def buyable(d: dict) -> bool:
    """A listing this client can pay: its contract is the known SRNetBalance deployment for its chain."""
    from . import chain
    L = d.get("listing") or {}
    try:
        known = chain.DEPLOYMENTS.get(int(L["chainid"]))
    except (KeyError, TypeError, ValueError):
        return False
    return bool(known) and str(L.get("contract", "")).lower() == known.lower()
