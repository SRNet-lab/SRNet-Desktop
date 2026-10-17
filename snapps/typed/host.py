"""Message-board primary host: validate -> spam gate -> append -> signed checkpoint;
author deletes, signed moderation records, retention, push to mirrors.  See mbhost/node.py
for the JSON-only HTTP API."""
from __future__ import annotations

import argparse
import json
import os
import threading
import time

import mbfmt as F
from .sync import http_json
from .record import BOARD
from .node import bind_ok, serve, url_of  # noqa: F401  (re-exported)
from .ratelimit import RateLimiter
from .store import Store

DEFAULT_POLICY = {
    "v": 1, "rules": "No illegal content, no spam, no doxxing. Hosts may hide posts "
                     "(signed and public; clients can override them locally) and remove "
                     "illegal content (signed and public; the text or file is deleted).",
    "appeal": "", "pow_bits": 18, "new_key_pow_bits": 21, "new_key_age_s": 86400,
    "rate": [[30, 3600], [200, 86400]], "new_key_rate": [[5, 3600]],
    "retention_days": 180, "checkpoint_valid_s": 3 * 3600,
}


def load_policy(path: str | None, board: str, overrides: dict | None = None) -> dict:
    p = dict(DEFAULT_POLICY)
    if path and os.path.exists(path):
        with open(path) as f:
            p.update(json.load(f))
    p.update(overrides or {})
    p["board"] = board
    return p


def env_body(env) -> dict:
    """Body of a stored typed record envelope ({"rec": {..., "body"}}), or {}."""
    b = ((env or {}).get("rec") or {}).get("body") if isinstance(env, dict) else None
    return b if isinstance(b, dict) else {}


MAX_PINS = 3
HEADER_FIELDS = {"description": 500, "rules": 1000}


class Host:
    def __init__(self, data_dir: str, board: str, host_key, delegation: dict,
                 policy: dict | None = None, mirrors: list[str] | None = None, clock=time.time,
                 codec=BOARD, rules=None):
        """codec: how records of this collection verify; rules(host, meta) -> error|None
        enforces per-type semantics (e.g. only sellers may list, only admins may protect)."""
        self.codec, self.rules = codec, rules
        os.makedirs(data_dir, exist_ok=True)
        self.board, self.key, self.delegation = board, host_key, delegation
        self._base_policy = load_policy(None, board, policy)
        self._header_path = os.path.join(data_dir, "header.json")
        self._header_mt, self._policy = None, None
        self.store = Store(os.path.join(data_dir, "log.sqlite3"))
        self.rl = RateLimiter([tuple(r) for r in self.policy["rate"]])
        self.rl_new = RateLimiter([tuple(r) for r in self.policy["new_key_rate"]])
        self.clock, self.mirrors = clock, list(mirrors or [])
        self._cp, self._cp_lock = None, threading.Lock()
        self._kick = threading.Event()
        self.push_report: dict = {}
        self.blobs = None  # set by the hub for file drops
        self.store.on_redact = self._on_redact

    def _on_redact(self, pid, env, tomb):
        """Host remove of a file: delete its blob bytes too (unless another live item uses it)."""
        if tomb.get("by") != "remove" or not self.blobs or not isinstance(env, dict):
            return
        sha = env_body(env).get("sha256")
        if sha and not any(env_body(e["env"]).get("sha256") == sha
                           for e in self.store.since(0, 1 << 30) if "env" in e):
            self.blobs.delete(sha)

    # ---- policy, incl. the room header (description + rules), signed via checkpoint ----
    @property
    def policy(self) -> dict:
        mt = os.path.getmtime(self._header_path) if os.path.exists(self._header_path) else None
        if self._policy is None or mt != self._header_mt:
            p = dict(self._base_policy)
            if mt is not None:
                with open(self._header_path) as f:
                    p.update({k: v for k, v in json.load(f).items() if k in HEADER_FIELDS})
            self._policy, self._header_mt = p, mt
        return self._policy

    @policy.setter
    def policy(self, p):
        self._base_policy, self._policy = p, None

    def set_header(self, description: str | None = None, rules: str | None = None) -> dict:
        """Edit the header every client shows at the top of the room.  It lives in the policy,
        whose hash the host signs into each checkpoint, so mirrors can't alter it."""
        h = {}
        if os.path.exists(self._header_path):
            with open(self._header_path) as f:
                h = json.load(f)
        for k, v in (("description", description), ("rules", rules)):
            if v is not None:
                if not isinstance(v, str) or len(v) > HEADER_FIELDS[k]:
                    raise F.FormatError(f"{k}: at most {HEADER_FIELDS[k]} characters")
                h[k] = v
        with open(self._header_path + ".tmp", "w") as f:
            json.dump(h, f)
        os.replace(self._header_path + ".tmp", self._header_path)
        self._policy = None
        self._kick.set()
        return {k: self.policy.get(k, "") for k in HEADER_FIELDS}

    def pins(self) -> list[str]:
        return active_pins(self.store.records_since(0, 1 << 30))

    # ---- spam gate ----
    def is_new_key(self, author: str) -> bool:
        first = self.store.first_post_ts(author)
        return first is None or self.clock() - first < self.policy["new_key_age_s"]

    def pow_for(self, author: str) -> int:
        return self.policy["new_key_pow_bits" if self.is_new_key(author) else "pow_bits"]

    # ---- writes ----
    def submit(self, env: dict) -> tuple[int, dict]:
        try:
            author = self.codec.verify(env, self.board).author  # cheap pre-check for PoW tier
            meta = self.codec.verify(env, self.board, now=self.clock(),
                                     pow_bits=self.pow_for(author), check_time=True)
            pid = meta.id
            if self.rules:
                err = self.rules(self, meta)
                if err:
                    return 400, {"ok": False, "error": err}
        except (F.FormatError, KeyError, TypeError, AttributeError) as e:
            return 400, {"ok": False, "error": str(e)}
        if self.store.has(pid):  # replay is idempotent, never re-appended
            return 200, {"ok": True, "id": pid, "seq": self.store.get(pid)["seq"], "dup": True}
        new = self.is_new_key(author)
        if not self.rl.allow(author) or (new and not self.rl_new.allow(author)):
            return 429, {"ok": False, "error": "rate limited" + (" (new key)" if new else "")}
        seq = self.store.append(self.codec.normalize(env), meta)
        self._kick.set()
        return 200, {"ok": True, "id": pid, "seq": seq}

    def delete(self, env: dict) -> tuple[int, dict]:
        try:
            d = F.verify_delete(env, self.board)
        except F.FormatError as e:
            return 400, {"ok": False, "error": str(e)}
        if abs(self.clock() - d["ts"]) > F.CLOCK_SKEW_S:
            return 400, {"ok": False, "error": "stale or future timestamp"}
        for pid in d["targets"]:
            e = self.store.get(pid)
            if e is None:
                return 400, {"ok": False, "error": f"unknown post {pid}"}
            if "env" in e and self.store.author_of(pid) != d["author"]:
                return 400, {"ok": False, "error": "can only delete your own posts"}
        rid = F.record_id(F.DELETE_DOMAIN, d)
        with self.store.lock:
            rseq = self.store.add_record("delete", env, rid)
            for pid in d["targets"]:
                self.store.redact(pid, {"by": "delete", "record": env})
        self._kick.set()
        return 200, {"ok": True, "rid": rid, "rseq": rseq, "dup": rseq is None}

    def moderate(self, action: str, target_type: str, target: str, reason: str) -> dict:
        """hide/unhide: signed public record, text kept, clients may override.
        remove (posts only): signed public record AND the text (or file blob) is deleted here
        at once, leaving a tombstone that carries the record; mirrors and clients redact too."""
        if action in ("remove", "pin") and not self.store.has(target):
            raise F.FormatError(f"unknown post {target}")
        if action == "pin":
            e = self.store.get(target)
            if "env" not in e:
                raise F.FormatError("can't pin a deleted or removed post")
            p = self.pins()
            if target in p:
                raise F.FormatError("already pinned")
            if len(p) >= MAX_PINS:
                raise F.FormatError(f"at most {MAX_PINS} pins; unpin one first")
        env = F.make_mod(self.key, self.board, action, target_type, target, reason,
                         ts=int(self.clock()))
        rid = F.record_id(F.MOD_DOMAIN, env["mod"])
        with self.store.lock:
            self.store.add_record("mod", env, rid)
            if action == "remove":
                self.store.redact(target, {"by": "remove", "record": env})
        self._kick.set()
        return env

    def apply_retention(self) -> int:
        days = self.policy.get("retention_days") or 0
        if days <= 0:
            return 0
        cut = int(self.clock() - days * 86400)
        return sum(self.store.redact(pid, {"by": "retention"}) for pid in self.store.older_than(cut))

    # ---- reads ----
    def checkpoint(self) -> dict:
        with self._cp_lock:
            seq, head = self.store.head()
            rseq = self.store.rhead()
            now = int(self.clock())
            c = self._cp and self._cp["checkpoint"]
            ph = F.policy_hash(self.policy)
            if not c or c["seq"] != seq or c["rseq"] != rseq or now - c["issued"] > 60 \
                    or c.get("policy") != ph:
                self._cp = F.make_checkpoint(self.key, self.board, seq, head,
                                             self.policy["checkpoint_valid_s"], now=now,
                                             policy=F.policy_hash(self.policy), rseq=rseq)
            return self._cp

    def info(self) -> dict:
        seq, _ = self.store.head()
        return {"v": 1, "board": self.board, "role": "primary", "host": F.pub_hex(self.key),
                "seq": seq, "rseq": self.store.rhead(), "pow_bits": self.policy["pow_bits"],
                "new_key_pow_bits": self.policy["new_key_pow_bits"],
                "max_body_chars": F.MAX_BODY_CHARS,
                **({"max_file_bytes": self.blobs.max_file_bytes,
                    "quota_bytes": self.blobs.quota_bytes} if self.blobs else {})}

    # ---- push to mirrors ----
    def push(self) -> dict:
        rep = {}
        for m in self.mirrors:
            try:
                for _ in range(1000):
                    _, mi = http_json(m + "/v1/info")
                    cp = self.checkpoint()
                    mseq, mr = int(mi.get("seq", 0)), int(mi.get("rseq", 0))
                    c = cp["checkpoint"]
                    same_pol = mi.get("policy") in (None, c.get("policy"))  # header edits
                    if mseq >= c["seq"] and mr >= c["rseq"] and (same_pol or _ > 0):
                        rep[m] = {"ok": True, "seq": mseq}
                        break
                    ents = [e for e in self.store.since(mseq, 500) if e["seq"] <= c["seq"]]
                    if ents and ents[-1]["seq"] < c["seq"]:
                        # too many entries for one push: publish an intermediate checkpoint
                        last = ents[-1]
                        cp = F.make_checkpoint(self.key, self.board, last["seq"], last["link"],
                                               self.policy["checkpoint_valid_s"],
                                               now=int(self.clock()),
                                               policy=F.policy_hash(self.policy), rseq=mr)
                        recs = []
                    else:
                        recs = [r for r in self.store.records_since(mr, 500)
                                if r["rseq"] <= c["rseq"]]
                    code, r = http_json(m + "/v1/publish", {
                        "delegation": self.delegation, "checkpoint": cp, "policy": self.policy,
                        "entries": ents, "records": recs}, timeout=15)
                    if code != 200:
                        rep[m] = {"ok": False, "error": r.get("error")}
                        break
            except (OSError, ValueError) as e:  # one dead mirror never stops the others
                rep[m] = {"ok": False, "error": str(e)}
        self.push_report = rep
        return rep

    def run_ticker(self, every: float = 30.0):
        def loop():
            while True:
                self._kick.wait(every)
                self._kick.clear()
                try:
                    self.apply_retention()
                    if self.mirrors:
                        self.push()
                except Exception as e:  # keep serving even if a tick fails
                    self.push_report = {"tick_error": str(e)}
        threading.Thread(target=loop, daemon=True).start()


def active_pins(records) -> list[str]:
    """Pinned post ids, oldest pin first, from host-signed pin/unpin records (max MAX_PINS)."""
    out = []
    for r in records:
        if r["kind"] != "mod":
            continue
        m = r["rec"]["mod"]
        if m["action"] == "pin" and m["target"] not in out:
            out.append(m["target"])
        elif m["action"] in ("unpin", "remove") and m["target"] in out:
            out.remove(m["target"])
    return out[-MAX_PINS:]


def load_or_make_keys(data_dir: str, board: str):
    """Phase-0 convenience: creates root.key and host.key in data_dir.  In production the
    root key is kept offline and only delegation.json is copied to the host (--delegation)."""
    os.makedirs(data_dir, exist_ok=True)

    def k(name):
        p = os.path.join(data_dir, name)
        if not os.path.exists(p):
            with open(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
                f.write(os.urandom(32))
        with open(p, "rb") as f:
            return F.key_from_seed(f.read())
    root, hk = k("root.key"), k("host.key")
    d = F.make_delegation(root, board, F.pub_hex(hk), int(time.time()) + F.MAX_DELEGATION_S)
    return root, hk, d


def load_host_key(data_dir: str):
    """Host key only (offline-root mode): creates host.key if missing, never a root key."""
    os.makedirs(data_dir, exist_ok=True)
    p = os.path.join(data_dir, "host.key")
    if not os.path.exists(p):
        with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(os.urandom(32))
    with open(p, "rb") as f:
        return F.key_from_seed(f.read())


def load_signed_delegation(path: str, board: str, host_key) -> dict:
    """Load a delegation signed elsewhere by an offline root; refuse if not for this host."""
    with open(path) as f:
        env = json.load(f)
    root = (env.get("delegation") or {}).get("root", "")
    if F.verify_delegation(env, root, board) != F.pub_hex(host_key):
        raise SystemExit(f"delegation {path} is for a different host key")
    return env


def main():
    ap = argparse.ArgumentParser(description="srnet-msgboard primary host")
    ap.add_argument("--data", required=True)
    ap.add_argument("--board", default="demo")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48800)
    ap.add_argument("--policy", help="policy JSON file (defaults in DEFAULT_POLICY)")
    ap.add_argument("--pow-bits", type=int)
    ap.add_argument("--new-key-pow-bits", type=int)
    ap.add_argument("--mirror", action="append", default=[], help="mirror URL to push to")
    ap.add_argument("--tick", type=float, default=30.0)
    ap.add_argument("--delegation", help="delegation JSON signed by an offline root (no root.key on host)")
    a = ap.parse_args()
    if a.delegation:
        hk = load_host_key(a.data)
        d = load_signed_delegation(a.delegation, a.board, hk)
        root_pin = d["delegation"]["root"]
    else:
        root, hk, d = load_or_make_keys(a.data, a.board)
        root_pin = F.pub_hex(root)
    ov = {k: v for k, v in (("pow_bits", a.pow_bits), ("new_key_pow_bits", a.new_key_pow_bits))
          if v is not None}
    h = Host(a.data, a.board, hk, d, load_policy(a.policy, a.board, ov), a.mirror)
    srv = serve(h, a.bind, a.port)
    h.run_ticker(a.tick)
    print(json.dumps({"url": url_of(srv), "board": a.board, "root_pin": root_pin,
                      "mirrors": a.mirror}), flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
