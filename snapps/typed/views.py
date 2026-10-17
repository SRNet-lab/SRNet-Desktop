"""Type semantics, computed identically by hosts (where cheap) and clients.

All functions take `items`: verified records in log order, each
{"id","seq","t","author","ts","body","refs"} — and the collection policy (for admins/sellers).
Templates only ever see the output of these functions."""
from __future__ import annotations

import hashlib

import mbfmt as F


def items_from_store(store, codec) -> list[dict]:
    out = []
    for e in store.since(0, 1 << 30):
        if "env" not in e:
            it = {"id": e["id"], "seq": e["seq"], "t": "tombstone", "by": e["tomb"].get("by")}
            if it["by"] == "remove":
                it["reason"] = ((e["tomb"].get("record") or {}).get("mod") or {}).get("reason", "")
            out.append(it)
            continue
        m = codec.verify(e["env"], None)  # already verified on entry; re-derive fields
        out.append({"id": m.id, "seq": e["seq"], "t": m.t, "author": m.author, "ts": m.ts,
                    "body": m.body, "refs": m.refs})
    return out


def key_hash(pub_hex: str) -> str:
    """Commitment to a recovery key: SHA-256 of the hex public key string."""
    return hashlib.sha256(("srnet-typed/recovery/v1\n" + pub_hex).encode()).hexdigest()


# ---- identity --------------------------------------------------------------------------
def identities(items: list[dict]) -> dict:
    """Resolve profiles, rotations (with recovery override), links.

    Rules:
      * id.rotate signed by old_key itself moves old_key -> new_key (or retires it if null).
      * A recovery rotation is signed by key R with key_hash(R) == next_key_hash committed in
        old_key's latest profile *before* the rotation; it overrides any rotation the old key
        made after that commitment (a thief holding the old key can't keep control).
      * Handles are never unique: display is always "name·<first 6 hex of key>".
    Returns {"profiles": {key: {...}}, "current": {key: final_key|None}, "labels": {key: str},
             "links": {key: [other_keys]}}"""
    profiles, commit, rot, links = {}, {}, {}, {}
    for it in items:
        t, a, b = it.get("t"), it.get("author"), it.get("body", {})
        if t == "id.profile":
            profiles[a] = dict(b, key=a, ts=it["ts"], seq=it["seq"])
            if b.get("next_key_hash") and a not in commit:
                # first commitment is binding: a thief who later publishes a new profile
                # cannot swap in their own recovery key (to change it, rotate to a new key)
                commit[a] = (b["next_key_hash"], it["seq"])
        elif t == "id.rotate":
            old = b["old_key"]
            if a == old:
                cur = rot.get(old)
                if cur is None or not cur["recovery"]:
                    rot[old] = {"to": b.get("new_key"), "seq": it["seq"], "recovery": False}
            else:
                c = commit.get(old)
                if c and key_hash(a) == c[0] and c[1] < it["seq"]:
                    rot[old] = {"to": b.get("new_key"), "seq": it["seq"], "recovery": True}
        elif t == "id.link":
            links.setdefault(a, set()).add(b["other_key"])
    current = {}

    def final(k, depth=0):
        if k is None or depth > 20:
            return k
        r = rot.get(k)
        return final(r["to"], depth + 1) if r else k
    for k in set(profiles) | set(rot):
        current[k] = final(k)
    labels = {}
    for k in set(profiles) | set(rot):
        f = current.get(k, k)
        name = (profiles.get(f) or profiles.get(k) or {}).get("name") or "anon"
        labels[k] = f"{name}·{(f or k)[:6]}" + (" (retired)" if f is None else "")
    mutual = {a: sorted(x for x in s if a in links.get(x, set())) for a, s in links.items()}
    return {"profiles": profiles, "current": current, "labels": labels,
            "rotations": rot, "links": mutual}


def label(ids, key):
    return ids["labels"].get(key) or f"anon·{key[:6]}"


# ---- rooms -----------------------------------------------------------------------------
def room_view(items, policy) -> dict:
    admins = set(policy.get("admins", []))
    ids = identities(items)
    nicks, topic, lines = {}, None, []
    for it in items:
        t = it.get("t")
        if t == "room.join":
            nicks[it["author"]] = it["body"]["nick"]
        elif t == "room.topic" and it["author"] in admins:
            topic = {"text": it["body"]["text"], "by": label(ids, it["author"]), "ts": it["ts"]}
        elif t == "room.msg":
            a = it["author"]
            lines.append({"id": it["id"], "seq": it["seq"], "ts": it["ts"], "author": a,
                          "who": f"{nicks.get(a) or ids['profiles'].get(a, {}).get('name') or 'anon'}·{a[:6]}",
                          "text": it["body"]["text"], "reply_to": (it["refs"] or [None])[0]})
    return {"topic": topic, "lines": lines}


# ---- wiki ------------------------------------------------------------------------------
def wiki_view(items, policy) -> dict:
    """Linear revision history per page.  A wiki.rev must name the current head as its parent
    (or no parent for the first revision); edits with a stale parent are kept as conflicts.
    Protected pages accept revisions/reverts only from admin keys (policy, hash-bound)."""
    admins = set(policy.get("admins", []))
    ids = identities(items)
    pages, revs = {}, {}
    for it in items:
        t, b = it.get("t"), it.get("body", {})
        if t not in ("wiki.page", "wiki.rev", "wiki.revert", "wiki.protect"):
            continue
        slug = b["slug"]
        p = pages.setdefault(slug, {"slug": slug, "title": slug, "head": None, "text": "",
                                    "protected": False, "history": [], "conflicts": []})
        a = it["author"]
        if t == "wiki.page":
            if not p["history"] and (not p["protected"] or a in admins):
                p["title"] = b["title"]
        elif t == "wiki.protect":
            if a in admins:
                p["protected"] = b["on"]
        else:
            if p["protected"] and a not in admins:
                p["conflicts"].append({"id": it["id"], "why": "page protected"})
                continue
            parent = (it["refs"] or [None])[0]
            if t == "wiki.rev":
                if parent != p["head"]:
                    p["conflicts"].append({"id": it["id"], "why": "stale parent"})
                    continue
                text = b["text"]
            else:  # revert: refs[0] is an earlier revision of this page
                tgt = revs.get(parent)
                if not tgt or tgt["slug"] != slug:
                    p["conflicts"].append({"id": it["id"], "why": "bad revert target"})
                    continue
                text = tgt["text"]
            revs[it["id"]] = {"slug": slug, "text": text}
            p["head"], p["text"] = it["id"], text
            p["history"].append({"id": it["id"], "t": t, "by": label(ids, a), "ts": it["ts"],
                                 "summary": b.get("summary") or ""})
    return {"pages": pages}


# ---- shop ------------------------------------------------------------------------------
def _listing_err(b):
    from .record import shop_listing_error
    return shop_listing_error(b)


def shop_view(items, policy) -> dict:
    """Listings only from policy sellers; prices and payment targets are passed through
    verbatim for the core's own payment sheet (templates never build payments)."""
    sellers = set(policy.get("sellers", []))
    ids = identities(items)
    listings, reviews = {}, {}
    for it in items:
        t, a, b = it.get("t"), it.get("author"), it.get("body", {})
        if t == "shop.listing" and a in sellers and not _listing_err(b):
            listings[it["id"]] = dict(b, id=it["id"], seller=a, seller_label=label(ids, a),
                                      ts=it["ts"], active=True)
        elif t == "shop.withdraw":
            lid = (it["refs"] or [None])[0]
            if lid in listings and listings[lid]["seller"] == a:
                listings[lid]["active"] = False
        elif t == "shop.order":
            lid = (it["refs"] or [None])[0]
            if lid in listings:
                listings[lid].setdefault("orders", []).append(
                    {"id": it["id"], "by": label(ids, a), "lease_id": b["lease_id"], "buyer": b["buyer"],
                     "ts": it["ts"]})
        elif t == "shop.review":
            lid = (it["refs"] or [None])[0]
            if lid in listings and listings[lid]["seller"] != a:
                reviews.setdefault(lid, {})[a] = {"by": label(ids, a), "rating": b["rating"],
                                                  "text": b.get("text") or ""}
    for lid, l in listings.items():
        rs = list(reviews.get(lid, {}).values())
        l["reviews"] = rs
        l.setdefault("orders", [])
        l["rating"] = round(sum(r["rating"] for r in rs) / len(rs), 2) if rs else None
    return {"listings": list(listings.values())}


# ---- drop ------------------------------------------------------------------------------
def drop_state(items) -> dict:
    """Full access state per file (internal to the core: includes key wraps).

    Only the file's author can grant or revoke. A revoke stops the owner's client from wrapping
    *future versions* to that key; it can't take back a copy someone already downloaded."""
    files = {it["id"]: it for it in items if it.get("t") == "drop.item"}
    st = {}
    for fid, it in files.items():
        b = it["body"]
        st[fid] = {"item": it, "owner": it["author"], "mode": b.get("mode") or "plain",
                   "wraps": {w["to"]: w for w in b.get("wraps") or []}, "revoked": set(),
                   "requests": [], "prev": (it.get("refs") or [None])[0], "next": None}
    for fid, s_ in st.items():
        p = s_["prev"]
        if p in st and st[p]["owner"] == s_["owner"]:
            st[p]["next"] = fid
    for it in items:
        t, refs = it.get("t"), it.get("refs") or []
        if t not in ("drop.grant", "drop.revoke", "drop.request") or not refs or refs[0] not in st:
            continue
        f = st[refs[0]]
        b = it["body"]
        if t == "drop.grant" and it["author"] == f["owner"] and b["sha256"] == f["item"]["body"]["sha256"]:
            f["wraps"].setdefault(b["wrap"]["to"], b["wrap"])
        elif t == "drop.revoke" and it["author"] == f["owner"]:
            f["revoked"].add(b["to"])
        elif t == "drop.request":
            f["requests"].append({"id": it["id"], "author": it["author"], "enc_key": b["enc_key"],
                                  "note": b.get("note") or "", "ts": it["ts"]})
    return st


def drop_view(items, policy, me: str | None = None, my_enc: str | None = None) -> dict:
    ids = identities(items)
    out = []
    for fid, f in drop_state(items).items():
        it, b = f["item"], f["item"]["body"]
        enc = f["mode"] != "plain"
        pending = [r for r in f["requests"] if r["enc_key"] not in f["wraps"] and r["enc_key"] not in f["revoked"]]
        out.append({"id": fid, "by": label(ids, it["author"]), "ts": it["ts"], "mode": f["mode"],
                    "encrypted": enc, "name": None if enc else b.get("name"),
                    "mime": None if enc else b.get("mime"), "size": b["size"], "note": b.get("note"),
                    "sha256": b["sha256"], "mine": me is not None and it["author"] == me,
                    "can_open": (not enc) or (my_enc in f["wraps"]),
                    "recipients": max(0, len(f["wraps"]) - 1), "revoked": len(f["revoked"]),
                    "requested": any(r["author"] == me for r in f["requests"]),
                    "requests": [{"id": r["id"], "by": label(ids, r["author"]), "note": r["note"]}
                                 for r in pending] if me == it["author"] else [],
                    "prev": f["prev"], "superseded_by": f["next"]})
    return {"items": out}


def directory_view(items, policy) -> dict:
    from .joins import LinkError, parse
    ids = identities(items)
    entries = {}
    for it in items:
        if it.get("t") != "directory.entry":
            continue
        b = it["body"]
        try:
            l = parse(b["link"])
        except LinkError:
            continue
        if not l.get("root") or it["author"] != l["root"]:
            continue  # only the collection's own root key can list it (host opt-in)
        entries[b["link"]] = {"id": it["id"], "link": b["link"], "title": b["title"], "desc": b.get("desc") or "",
                              "host": l["host"], "kind": l["kind"], "name": l["name"],
                              "by": label(ids, it["author"]), "ts": it["ts"]}
    return {"entries": sorted(entries.values(), key=lambda e: e["title"].lower())}


VIEWS = {"directory": directory_view, "room": room_view, "wiki": wiki_view, "shop": shop_view, "drop": drop_view,
         "id": lambda items, policy: {}}


def host_rules(host, meta) -> str | None:
    """Host-side admission rules (clients apply the same rules in the views)."""
    pol = host.policy
    if meta.t in ("shop.listing", "shop.withdraw") and meta.author not in pol.get("sellers", []):
        return "only the store's seller keys may list or withdraw"
    if meta.t == "drop.item" and pol.get("max_file_bytes"):
        from .filecrypt import max_cipher_len
        lim = pol["max_file_bytes"] if (meta.body.get("mode") or "plain") == "plain" \
            else max_cipher_len(pol["max_file_bytes"])
        if meta.body["size"] > lim:
            return f"file too large for this drop (max {pol['max_file_bytes']} bytes)"
    if meta.t in ("wiki.protect", "room.topic") and meta.author not in pol.get("admins", []):
        return f"{meta.t} needs an admin key"
    if meta.t == "shop.listing":
        from .record import TESTNET_CHAINS, shop_listing_error
        err = shop_listing_error(meta.body)
        if err:
            return err
        if meta.body.get("currency") != "SRNET" and meta.body["chain_id"] not in TESTNET_CHAINS \
                and not pol.get("allow_mainnet"):
            return "payments are testnet-only on this store"
    return None
