"""Hub: one service hosting many typed collections (rooms, wiki, store, drop, profiles),
each with its own log, checkpoints, delegation (signed by the hub's collection root key)
and policy.  Routes /c/<collection>/v1/... ; GET /v1/collections lists them.

Run:  python3 -m typed.hub --data DIR --port 48810 \\
          --coll room:lobby --coll room:dev --coll wiki:wiki --coll shop:store \\
          --coll drop:files --coll id:people
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time

import mbfmt as F
from .host import Host, load_policy
from .node import serve, url_of
from .record import codec_for
from .views import host_rules

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
KIND_POLICY = {  # lighter limits for rooms; heavier for wiki/shop; 
    "room": {"rate": [[60, 600], [600, 86400]], "pow_bits": 12, "new_key_pow_bits": 14},
    "wiki": {"rate": [[20, 3600]], "pow_bits": 14, "new_key_pow_bits": 16},
    "shop": {"rate": [[20, 3600]], "pow_bits": 14, "new_key_pow_bits": 16},
    "drop": {"rate": [[10, 3600]], "pow_bits": 14, "new_key_pow_bits": 16,
             "max_file_bytes": 25 * 1024 * 1024, "quota_bytes": 0},
    "id": {"rate": [[10, 3600]], "pow_bits": 12, "new_key_pow_bits": 14},
    "directory": {"rate": [[20, 3600]], "pow_bits": 12, "new_key_pow_bits": 14},
}


UPLOAD_DOMAIN = "srnet-typed/upload/v1"
UP_CHUNK = 1 << 20  # largest chunk per request


class BlobStore:
    """Content-addressed blobs for file drops: stored and served by SHA-256 only.

    Limits come from the drop's (checkpoint-signed) policy: max_file_bytes caps the file,
    quota_bytes (0 = none) caps the bytes each uploader key may store.  Large files arrive
    in chunks (/v1/upload); a partial upload is kept and resumes at the byte it stopped."""

    def __init__(self, d, max_file_bytes=None, quota_bytes=0, coll=""):
        from .filecrypt import DEFAULT_MAX_FILE, max_cipher_len
        self.d, self.coll = d, coll
        self.max_file_bytes = int(max_file_bytes or DEFAULT_MAX_FILE)
        self.max_bytes = max_cipher_len(self.max_file_bytes)
        self.quota_bytes = int(quota_bytes or 0)
        self.up = os.path.join(d, ".up")
        os.makedirs(self.up, exist_ok=True)
        self._lock = threading.Lock()
        self._acct_path = os.path.join(d, ".owners.json")
        self._acct = {}
        if os.path.exists(self._acct_path):
            with open(self._acct_path) as f:
                self._acct = json.load(f)

    def _save(self):
        with open(self._acct_path + ".tmp", "w") as f:
            json.dump(self._acct, f)
        os.replace(self._acct_path + ".tmp", self._acct_path)

    def used(self, author):
        return sum(v["size"] for v in self._acct.values() if v["author"] == author)

    def _commit(self, b: bytes):
        h = hashlib.sha256(b).hexdigest()
        p = os.path.join(self.d, h)
        if not os.path.exists(p):
            with open(p + ".tmp", "wb") as f:
                f.write(b)
            os.replace(p + ".tmp", p)
        return h

    def put(self, b: bytes):
        """Single-shot unsigned upload (older clients).  Refused when a quota is set."""
        if self.quota_bytes:
            return 403, {"ok": False, "error": "this host has quotas: use signed /v1/upload"}
        if len(b) > self.max_bytes:
            return 413, {"ok": False, "error": "size", "max_file_bytes": self.max_file_bytes}
        return 200, {"ok": True, "sha256": self._commit(b), "size": len(b)}

    # ---- chunked, resumable ----
    def start(self, env: dict, now: float):
        try:
            r, sig = env["upload"], env["sig"]
            if set(r) != {"v", "coll", "author", "sha256", "size", "ts"} or r["coll"] != self.coll:
                raise ValueError("fields")
            if not re.fullmatch(r"[0-9a-f]{64}", r["sha256"]) or not isinstance(r["size"], int):
                raise ValueError("sha256/size")
            if not F._verify(r["author"], sig, F.canonical(UPLOAD_DOMAIN, r)):
                raise ValueError("bad signature")
            if abs(now - r["ts"]) > F.CLOCK_SKEW_S:
                raise ValueError("stale timestamp")
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            return 400, {"ok": False, "error": f"bad upload request: {e}"}
        sha, size, who = r["sha256"], r["size"], r["author"]
        if size <= 0 or size > self.max_bytes:
            return 413, {"ok": False, "error": f"file too large for this host (max "
                         f"{self.max_file_bytes} bytes)", "max_file_bytes": self.max_file_bytes}
        with self._lock:
            if os.path.exists(os.path.join(self.d, sha)):
                return 200, {"ok": True, "sha256": sha, "have": size, "done": True}
            if self.quota_bytes and self.used(who) + size > self.quota_bytes:
                return 403, {"ok": False, "error": "upload quota exceeded",
                             "quota_bytes": self.quota_bytes, "used": self.used(who)}
            meta = os.path.join(self.up, sha + ".json")
            if not os.path.exists(meta):
                with open(meta, "w") as f:
                    json.dump({"author": who, "size": size}, f)
                open(os.path.join(self.up, sha + ".part"), "wb").close()
            have = os.path.getsize(os.path.join(self.up, sha + ".part"))
        return 200, {"ok": True, "sha256": sha, "have": have, "done": False, "chunk": UP_CHUNK}

    def status(self, sha):
        if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
            return 400, {"error": "bad hash"}
        if os.path.exists(os.path.join(self.d, sha)):
            return 200, {"sha256": sha, "done": True}
        p = os.path.join(self.up, sha + ".part")
        if not os.path.exists(p):
            return 404, {"error": "no such upload"}
        return 200, {"sha256": sha, "have": os.path.getsize(p), "done": False}

    def chunk(self, sha, off: int, data: bytes):
        if not re.fullmatch(r"[0-9a-f]{64}", sha or "") or len(data) > UP_CHUNK:
            return 400, {"ok": False, "error": "bad chunk"}
        with self._lock:
            mp, pp = os.path.join(self.up, sha + ".json"), os.path.join(self.up, sha + ".part")
            if not os.path.exists(mp):
                return 404, {"ok": False, "error": "start the upload first"}
            with open(mp) as f:
                meta = json.load(f)
            have = os.path.getsize(pp)
            if off != have:  # resume point mismatch: tell the client where to continue
                return 409, {"ok": False, "error": "offset", "have": have}
            if have + len(data) > meta["size"]:
                return 413, {"ok": False, "error": "more bytes than announced"}
            with open(pp, "ab") as f:
                f.write(data)
            have += len(data)
            if have < meta["size"]:
                return 200, {"ok": True, "have": have, "done": False}
            with open(pp, "rb") as f:
                b = f.read()
            os.remove(pp)
            os.remove(mp)
            if hashlib.sha256(b).hexdigest() != sha:
                return 400, {"ok": False, "error": "hash mismatch; upload discarded"}
            self._commit(b)
            self._acct[sha] = {"author": meta["author"], "size": len(b)}
            self._save()
            return 200, {"ok": True, "have": have, "done": True, "sha256": sha}

    def delete(self, h) -> bool:
        if not re.fullmatch(r"[0-9a-f]{64}", h or ""):
            return False
        p = os.path.join(self.d, h)
        with self._lock:
            if self._acct.pop(h, None):
                self._save()
        if os.path.exists(p):
            os.remove(p)
            return True
        return False

    def get(self, h):
        if not re.fullmatch(r"[0-9a-f]{64}", h or ""):
            return None
        p = os.path.join(self.d, h)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()


def sign_upload(key, coll, sha, size, ts=None) -> dict:
    r = {"v": 1, "coll": coll, "author": F.pub_hex(key), "sha256": sha, "size": size,
         "ts": int(time.time()) if ts is None else int(ts)}
    return {"upload": r, "sig": key.sign(F.canonical(UPLOAD_DOMAIN, r)).hex()}


class Hub:
    def __init__(self, data_dir, root_key, host_key, colls: list[tuple[str, str]],
                 policy_extra: dict | None = None, clock=time.time, pol_override=None,
                 delegations: dict | None = None):
        self.nodes, self.kinds = {}, {}
        for kind, name in colls:
            if not NAME_RE.match(name) or kind not in KIND_POLICY:
                raise SystemExit(f"bad collection {kind}:{name}")
            if delegations is not None:
                d = delegations[name]
            else:
                d = F.make_delegation(root_key, name, F.pub_hex(host_key),
                                      int(clock()) + F.MAX_DELEGATION_S)
            pol = dict(KIND_POLICY[kind], kind=kind, **(policy_extra or {}))
            pol.update((pol_override or {}).get(kind, {}))
            h = Host(os.path.join(data_dir, name), name, host_key, d, load_policy(None, name, pol),
                     clock=clock, codec=codec_for(kind), rules=host_rules)
            if kind == "drop":
                h.blobs = BlobStore(os.path.join(data_dir, name, "blobs"),
                                    h.policy.get("max_file_bytes"), h.policy.get("quota_bytes"), name)
            self.nodes[name], self.kinds[name] = h, kind

    def __call__(self, path):
        parts = path.split("/", 3)
        if len(parts) >= 4 and parts[1] == "c" and parts[2] in self.nodes:
            return self.nodes[parts[2]], "/" + parts[3]
        return None, None

    def collections(self):
        return {"collections": [{"name": n, "kind": self.kinds[n]} for n in self.nodes]}


def main():
    from .host import load_or_make_keys
    ap = argparse.ArgumentParser(description="typed-SNApp hub")
    ap.add_argument("--data", required=True)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=48810)
    ap.add_argument("--coll", action="append", required=True, help="kind:name")
    ap.add_argument("--admin", action="append", default=[], help="admin key hex")
    ap.add_argument("--seller", action="append", default=[], help="seller key hex")
    ap.add_argument("--max-file-mb", type=float, default=25,
                    help="largest file a drop accepts (MiB, default 25)")
    ap.add_argument("--quota-mb", type=float, default=0,
                    help="bytes each uploader key may store per drop (MiB, 0 = no quota)")
    ap.add_argument("--delegation-dir",
                    help="dir of <collection>.json delegations signed by an offline root")
    a = ap.parse_args()
    colls = [tuple(c.split(":", 1)) for c in a.coll]
    if a.delegation_dir:
        from .host import load_host_key, load_signed_delegation
        hk, root = load_host_key(a.data), None
        dels = {n: load_signed_delegation(os.path.join(a.delegation_dir, n + ".json"), n, hk)
                for _, n in colls}
        pins = {e["delegation"]["root"] for e in dels.values()}
        if len(pins) != 1:
            raise SystemExit("all collections must share one root")
        root_pin = pins.pop()
    else:
        root, hk, _ = load_or_make_keys(a.data, "hub")
        dels, root_pin = None, F.pub_hex(root)
    drop_pol = {"max_file_bytes": int(a.max_file_mb * 1024 * 1024),
                "quota_bytes": int(a.quota_mb * 1024 * 1024)}
    hub = Hub(a.data, root, hk, colls, {"admins": a.admin, "sellers": a.seller},
              delegations=dels, pol_override={"drop": drop_pol})
    srv = serve(hub, a.bind, a.port)
    print(json.dumps({"url": url_of(srv), "root_pin": root_pin,
                      "collections": hub.collections()["collections"]}), flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
