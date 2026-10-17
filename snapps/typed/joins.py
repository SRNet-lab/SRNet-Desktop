"""Join links and host probing for the typed client.

Link format (what Share produces and what users paste):
    srnet://<host>.sesh/<kind>/<collection>#root=<64-hex root key>
A bare `<host>.sesh` (or `srnet://<host>.sesh`) lists the collections that host offers.
Hosts are .sesh names (served over Session Router as http://<name>.sesh) or loopback
(127.0.0.1 / localhost / [::1] with a port) for local testing. Everything else is refused,
so a pasted link can't point the core at arbitrary web hosts, file paths or schemes.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

import mbfmt as F

from .sync import http_json

KINDS = ("board", "room", "wiki", "shop", "drop", "id", "directory", "mail")  # mail: a mail host, not a collection
SESH_RE = re.compile(r"^[ybndrfg8ejkmcpqxot1uwisza345h769]{52}\.sesh$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
LOOP_RE = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\]):([1-9][0-9]{0,4})$")
MAX_LINK = 400


class LinkError(ValueError):
    pass


def _host_ok(h: str) -> bool:
    if SESH_RE.match(h):
        return True
    m = LOOP_RE.match(h)
    return bool(m) and int(m.group(2)) <= 65535


def parse(link: str) -> dict:
    """-> {"host", "kind"|None, "name"|None, "root"|None}. Raises LinkError."""
    if not isinstance(link, str):
        raise LinkError("not a link")
    if len(link) > MAX_LINK + 40:
        raise LinkError("link is too long")
    if any(unicodedata.category(ch)[0] in "CZ" and ch != " " for ch in link):
        raise LinkError("link contains control characters")
    s = link.strip(" ")
    if not s or len(s) > MAX_LINK:
        raise LinkError("link is empty or too long")
    if " " in s:
        raise LinkError("link contains spaces or control characters")
    if s != unicodedata.normalize("NFKC", s) or not s.isascii():
        raise LinkError("link must be plain ASCII")
    frag = ""
    if "#" in s:
        s, frag = s.split("#", 1)
    if s.startswith("srnet://"):
        s = s[len("srnet://"):]
    elif "://" in s:
        raise LinkError("only srnet:// links or .sesh addresses")
    if "?" in s or "@" in s or "\\" in s or "%" in s:
        raise LinkError("unexpected characters in link")
    parts = s.split("/")
    host = parts[0].lower()
    if not _host_ok(host):
        raise LinkError("host must be a .sesh address")
    rest = [p for p in parts[1:]]
    if rest and rest[-1] == "":
        rest = rest[:-1]
    kind = name = None
    if rest:
        if len(rest) != 2:
            raise LinkError("expected srnet://<host>/<type>/<collection>")
        kind, name = rest
        if kind not in KINDS:
            raise LinkError(f"unknown type {kind!r}")
        if not NAME_RE.match(name):
            raise LinkError("bad collection name")
    root = None
    if frag:
        if not frag.startswith("root=") or not HEX64.match(frag[5:]):
            raise LinkError("fragment must be #root=<64 hex>")
        root = frag[5:]
    return {"host": host, "kind": kind, "name": name, "root": root}


def make(host: str, kind: str, name: str, root: str) -> str:
    if not (_host_ok(host) and kind in KINDS and NAME_RE.match(name) and HEX64.match(root)):
        raise LinkError("can't make a link from these values")
    return f"srnet://{host}/{quote(kind)}/{quote(name)}#root={root}"


def base_url(host: str) -> str:
    return f"http://{host}"


def candidates(host: str, kind: str, name: str) -> list[str]:
    """Hub collections live at /c/<name>; a single-collection host serves at its root."""
    b = base_url(host)
    return [f"{b}/c/{name}", b]


def offered(host: str) -> list[dict]:
    """Collections a host lists (hubs answer /v1/collections)."""
    _, r = http_json(base_url(host) + "/v1/collections")
    out = []
    for c in (r or {}).get("collections", []):
        if isinstance(c, dict) and c.get("kind") in KINDS and NAME_RE.match(str(c.get("name", ""))):
            out.append({"kind": c["kind"], "name": c["name"]})
    return out[:100]


def probe(host: str, kind: str, name: str) -> dict:
    """Fetch the host's signed delegation for a collection. Verifies the signature against the
    root key *it names*; the caller decides whether that root is trusted (link pin / TOFU)."""
    if kind == "mail":
        return probe_mail(host, name)
    errors = []
    for url in candidates(host, kind, name):
        try:
            code, env = http_json(url + "/v1/delegation")
            d = (env or {}).get("delegation") or {}
            if code != 200 or d.get("board") != name or not HEX64.match(str(d.get("root", ""))):
                raise LinkError("no delegation for this collection")
            F.verify_delegation(env, d["root"], name)
            _, pol = http_json(url + "/v1/policy")
            pk = (pol or {}).get("kind") or ("board" if kind == "board" else None)
            if pk and pk != kind:
                raise LinkError(f"host says this is a {pk}, not a {kind}")
            return {"url": url, "root": d["root"], "host_key": d.get("host"), "expires": d.get("expires"),
                    "rules": str((pol or {}).get("rules", ""))[:500]}
        except (LinkError, F.FormatError, OSError, ValueError, KeyError, TypeError) as e:
            errors.append(f"{url}: {e}")
    raise LinkError("host didn't verify: " + "; ".join(errors))


def probe_mail(host: str, name: str) -> dict:
    """A mail host joins by link too: /v1/mail/info must carry a delegation for <name> signed by
    the root key it names (the hosting pack's `link` writes it). The caller checks that root."""
    url = base_url(host)
    try:
        code, info = http_json(url + "/v1/mail/info")
        env = (info or {}).get("delegation") or {}
        d = env.get("delegation") or {}
        if code != 200 or (info or {}).get("role") != "mail" or d.get("board") != name \
                or not HEX64.match(str(d.get("root", ""))):
            raise LinkError("this host publishes no signed mail delegation")
        F.verify_delegation(env, d["root"], name)
    except (LinkError, F.FormatError, OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise LinkError(f"mail host didn't verify: {url}: {e}")
    lim = info.get("limits") or {}
    rules = "mail host · new inboxes " + ("open" if lim.get("open_registration", True) else "closed")
    if lim.get("ttl_default_s"):
        rules += f" · mail kept {int(lim['ttl_default_s']) // 86400 or 1} day(s)"
    return {"url": url, "root": d["root"], "host_key": d.get("host"), "expires": d.get("expires"),
            "rules": rules}
