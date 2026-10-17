"""Deliver a buyer's signed lease claim to the seller over Session Router.

Prefer the listing's signed `claim_url` when present (seller pack publishes
`http://<claim-host>.sesh/claim`). Otherwise fall back to
`http://<listing seller .sesh>:<claim_port>/v1/claim` (claim_port defaults to 8650).

Delivery is retried with backoff; the seller's receiver is idempotent, so re-sending
is always safe. Returns the seller's acknowledgment:
  {"delivered": bool, "status": "queued"|"admitted"|"rejected"|"conflict"|"undelivered", "reason": ...}
If delivery fails the claim stays in the local spool and `srnet deliver` retries it later.
"""
from __future__ import annotations

import http.client
import json
import re
import time
import urllib.parse

DEFAULT_CLAIM_PORT = 8650
# http://<52 z-base32>.sesh[:port]/claim  or  .../v1/claim
_CLAIM_URL_RE = re.compile(
    r"^http://([a-z0-9]{52}\.sesh)(?::(\d{1,5}))?(/claim|/v1/claim)$"
)


def claim_url(listing: dict) -> str:
    """Build the claim POST URL from a signed listing body."""
    raw = (listing.get("claim_url") or "").strip()
    if raw:
        m = _CLAIM_URL_RE.match(raw)
        if not m:
            raise ValueError("listing claim_url is not a usable .sesh claim endpoint")
        host, port, path = m.group(1), m.group(2), m.group(3)
        if port:
            p = int(port)
            if not (1 <= p <= 65535):
                raise ValueError("listing claim_url has a bad port")
            return f"http://{host}:{p}{path}"
        return f"http://{host}{path}"
    seller = listing["seller"]
    port = int(listing.get("claim_port") or DEFAULT_CLAIM_PORT)
    if not (seller.endswith(".sesh") and 1 <= port <= 65535):
        raise ValueError("listing has no usable seller address")
    return f"http://{seller}:{port}/v1/claim"


def http_transport(url: str, body: bytes | None, timeout: float = 30, dns: str | None = None) -> tuple[int, dict]:
    """POST (body) or GET (None) over plain HTTP; .sesh names resolve via Session Router's DNS."""
    u = urllib.parse.urlparse(url)
    host = u.hostname or ""
    if host.endswith(".sesh"):
        import seshdns
        ip = seshdns.resolve(host, dns)
    elif host in ("127.0.0.1", "::1", "localhost"):
        ip = host
    else:
        raise ValueError("claims go only to .sesh sellers (or loopback for tests)")
    c = http.client.HTTPConnection(ip, u.port or 80, timeout=timeout)
    try:
        h = {"Host": u.netloc}
        if body is not None:
            h["Content-Type"] = "application/json"
        c.request("POST" if body is not None else "GET", u.path, body=body, headers=h)
        r = c.getresponse()
        data = r.read(4096)
    finally:
        c.close()
    try:
        return r.status, json.loads(data or b"{}")
    except ValueError:
        return r.status, {}


def deliver(claim: dict, listing: dict, *, transport=http_transport, attempts: int = 6,
            backoff: float = 2.0, wait_admit: float = 60, poll: float = 3, sleep=time.sleep,
            url: str | None = None, dns: str | None = None) -> dict:
    url = url or claim_url(listing)
    body = json.dumps(claim).encode()
    last = "no attempt"
    for i in range(attempts):
        if i:
            sleep(min(backoff * 2 ** (i - 1), 60))
        try:
            code, r = transport(url, body, dns=dns) if transport is http_transport else transport(url, body)
        except (OSError, ValueError) as e:
            last = f"{e.__class__.__name__}: {e}"
            if isinstance(e, ValueError) and "only to .sesh" in str(e):
                break
            continue
        if code == 429 or code >= 500:
            last = f"HTTP {code}"
            continue
        if code in (400, 409):
            return {"delivered": code == 409, "status": r.get("status", "invalid"), "reason": r.get("reason"),
                    "url": url}
        st = r.get("status")
        if st is None and 200 <= code < 300:
            # Older seller packs (srnet-claim/1) answer 202 with a plain-text body and no status
            # poll endpoint: the claim was accepted, admission is not reported.
            st = "received"
        if st == "queued" and wait_admit > 0:
            end = time.monotonic() + wait_admit
            while time.monotonic() < end and st == "queued":
                sleep(poll)
                try:
                    _, g = (transport(f"{url.rstrip('/')}/{claim['hold_id']}", None, dns=dns) if transport is http_transport
                            else transport(f"{url.rstrip('/')}/{claim['hold_id']}", None))
                    st, r = g.get("status", st), g
                except (OSError, ValueError):
                    break
        return {"delivered": True, "status": st, "reason": r.get("reason"), "url": url}
    return {"delivered": False, "status": "undelivered", "reason": last, "url": url}
