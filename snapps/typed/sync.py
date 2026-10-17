"""Verification shared by the client, mirrors and the publish (push) path, plus HTTP helpers.

sync_once, for each host in order until one verifies:
  delegation (root pin) -> host key -> checkpoint (sig, expiry, rollback/equivocation)
  -> /log since our cursor up to checkpoint.seq: each entry's post signature + content id,
     seq continuity and chain link (tombstones carry a signed author delete record, a
     host-signed remove record or a retention marker; links cover ids only, so the chain still verifies)
  -> final head == checkpoint.head
  -> /records since our record cursor up to checkpoint.rseq, each signature-checked
  -> policy matches checkpoint.policy hash.
Only a fully verified batch is committed; anything else fails over to the next host.
"""
from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

import mbfmt as F
from .record import BOARD

HTTP_TIMEOUT = 5
MAX_RESP = 4 << 20


class SyncError(Exception):
    pass


def http_json(url: str, data: dict | None = None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ct = r.headers.get("Content-Type", "")
            if not ct.startswith("application/json"):
                raise SyncError(f"host sent non-JSON content ({ct!r}); refused")
            return r.status, json.loads(r.read(MAX_RESP))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read(MAX_RESP))
        except ValueError:
            return e.code, {"error": str(e)}


def check_tomb(pid: str, tomb: dict, board: str, host_pub: str | None = None) -> dict:
    if not isinstance(tomb, dict):
        raise SyncError("bad tombstone")
    if tomb.get("by") == "remove":
        if host_pub is None:
            raise SyncError("remove tombstone needs the host key")
        m = F.verify_mod(tomb.get("record"), host_pub, board)
        if m["action"] != "remove" or m["target_type"] != "post" or m["target"] != pid:
            raise SyncError("tombstone remove record does not cover this post")
        return {"by": "remove", "record": tomb["record"]}
    if tomb.get("by") == "delete":
        r = F.verify_delete(tomb.get("record"), board)
        if pid not in r["targets"]:
            raise SyncError("tombstone delete record does not cover this post")
        return {"by": "delete", "record": tomb["record"]}
    if tomb.get("by") == "retention":
        return {"by": "retention"}
    raise SyncError("unknown tombstone")


def verify_segment(entries, start_seq, start_head, board, codec=BOARD, host_pub=None):
    """Returns (seq, head, [(entry, meta|None)])."""
    seq, head, out = start_seq, start_head, []
    for e in entries:
        pid = e.get("id")
        if "tomb" in e:
            if not isinstance(pid, str):
                raise SyncError("tomb id")
            ent, meta = {"tomb": check_tomb(pid, e["tomb"], board, host_pub)}, None
        else:
            meta = codec.verify(e["env"], board)
            pid = meta.id
            if e.get("id") != pid or e["env"].get("id", pid) != pid:
                raise SyncError(f"entry {e.get('seq')}: id mismatch")
            ent = {"env": codec.normalize(e["env"])}
        if e.get("seq") != seq + 1:
            raise SyncError(f"entry {e.get('seq')}: seq gap")
        link = F.chain_next(head, e["seq"], pid)
        if e.get("link") != link:
            raise SyncError(f"entry {e['seq']}: chain link mismatch (forked or edited log)")
        seq, head = e["seq"], link
        out.append((dict(ent, seq=seq, id=pid, link=link), meta))
    return seq, head, out


def verify_record(r: dict, host_pub: str, board: str) -> dict:
    kind, rec = r.get("kind"), r.get("rec")
    if kind == "mod":
        obj, dom = F.verify_mod(rec, host_pub, board), F.MOD_DOMAIN
    elif kind == "delete":
        obj, dom = F.verify_delete(rec, board), F.DELETE_DOMAIN
    else:
        raise SyncError("unknown record kind")
    rid = F.record_id(dom, obj)
    if r.get("rid") != rid:
        raise SyncError("record id mismatch")
    return {"rseq": r["rseq"], "rid": rid, "kind": kind, "rec": rec}


def verify_records(recs, start_rseq, host_pub, board):
    out, n = [], start_rseq
    for r in recs:
        if r.get("rseq") != n + 1:
            raise SyncError("record seq gap")
        out.append(verify_record(r, host_pub, board))
        n += 1
    return out


def apply_record(store, r) -> None:
    """Store a verified record; author deletes also redact our local copy (author must match);
    host removes (already verified against the delegated host key) always redact it."""
    if store.add_record(r["kind"], r["rec"], r["rid"], r["rseq"]) is None:
        return
    if r["kind"] == "delete":
        d = r["rec"]["delete"]
        for pid in d["targets"]:
            if store.author_of(pid) == d["author"]:
                store.redact(pid, {"by": "delete", "record": r["rec"]})
    elif r["kind"] == "mod" and r["rec"]["mod"]["action"] == "remove":
        store.redact(r["rec"]["mod"]["target"], {"by": "remove", "record": r["rec"]})


def accept_batch(store, pin, root_pin, board, delegation, cpe, fetch_entries, fetch_records,
                 policy=None, now=None, codec=BOARD):
    """Verify and commit one batch.  fetch_entries(seq)->list, fetch_records(rseq)->list."""
    host_pub = F.verify_delegation(delegation, root_pin, board, now=now)
    cp = F.verify_checkpoint(cpe, host_pub, board, now=now)
    pin.check(cp)
    if policy is not None and cp.get("policy") and F.policy_hash(policy) != cp["policy"]:
        raise SyncError("policy does not match checkpoint")
    seq, head = store.head()
    if cp["seq"] < seq:
        raise SyncError("host is behind our verified state")
    new = []
    while seq < cp["seq"]:
        ents = [e for e in fetch_entries(seq) if e.get("seq", 0) <= cp["seq"]]
        if not ents:
            raise SyncError("log shorter than checkpoint")
        seq, head, out = verify_segment(ents, seq, head, board, codec, host_pub)
        new += out
    if head != cp["head"]:
        raise SyncError("chain head does not match signed checkpoint")
    rseq, recs = store.rhead(), []
    want = cp.get("rseq", 0)
    while rseq < want:
        rs = [r for r in fetch_records(rseq) if r.get("rseq", 0) <= want]
        if not rs:
            raise SyncError("records shorter than checkpoint")
        recs += verify_records(rs, rseq, host_pub, board)
        rseq = recs[-1]["rseq"]
    with store.lock:
        for e, meta in new:
            store.import_entry(e, meta)
        for r in recs:
            apply_record(store, r)
    pin.seq, pin.head = cp["seq"], cp["head"]
    return {"added": len(new), "records": len(recs), "seq": cp["seq"], "host_pub": host_pub}


def sync_once(store, hosts, root_pin, board, pin, now=None, codec=BOARD) -> dict:
    errors = {}
    for url in hosts:
        try:
            _, d = http_json(url + "/v1/delegation")
            _, cpe = http_json(url + "/v1/checkpoint")
            _, pol = http_json(url + "/v1/policy")
            r = accept_batch(
                store, pin, root_pin, board, d, cpe,
                lambda s: http_json(f"{url}/v1/log?since={s}&limit=500")[1].get("entries", []),
                lambda s: http_json(f"{url}/v1/records?since={s}&limit=500")[1].get("records", []),
                policy=pol, now=now, codec=codec)
            return dict(r, host=url, checkpoint=cpe, delegation=d, policy=pol, errors=errors)
        except (SyncError, F.FormatError, OSError, ValueError, KeyError, TypeError,
                AttributeError) as e:
            errors[url] = f"{type(e).__name__}: {e}"
    raise SyncError(f"no host verified: {errors}")


def submit(hosts, env, path="/v1/submit") -> dict:
    """Try hosts in order.  400/429 means the item itself was refused (don't retry elsewhere).
    ok False + 'unreachable' True means no host took it (caller may queue it)."""
    errors = {}
    for url in hosts:
        try:
            code, r = http_json(url + path, env)
            if code == 200 and r.get("ok"):
                return dict(r, host=url)
            if code in (400, 429):
                return dict(r, host=url, status=code)
            errors[url] = r
        except (OSError, SyncError, ValueError) as e:
            errors[url] = str(e)
    return {"ok": False, "unreachable": True, "error": "no host accepted", "detail": errors}
