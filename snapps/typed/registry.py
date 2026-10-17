"""Signed template registry v0.

entry  = {"template","ver","types","files":{name:sha256},"bundle_sha256","source_commit",
          "summary","issued_at","expires_at","key_id"}   signed: srnet-typed/registry.entry/v1
withdraw list = {"seq","issued_at","withdrawn":[{"template","ver","reason"}],"key_id"}
                                                         signed: srnet-typed/registry.withdraw/v1
Client rules: only entries signed by the pinned registry key run; files must hash to the
entry; updates are offered and need explicit user approval; a lower or equal version is
refused (rollback) unless the installed one is withdrawn; a withdrawn version is never loaded
and the client falls back to the newest good version it still holds.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import mbfmt as F

ENTRY_DOMAIN = "srnet-typed/registry.entry/v1"
WITHDRAW_DOMAIN = "srnet-typed/registry.withdraw/v1"
VER_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
FILE_RE = re.compile(r"^[a-z0-9_-]{1,32}\.(html|js|css)$")
TEMPLATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class RegistryError(Exception):
    pass


def ver_t(v: str):
    m = VER_RE.match(v or "")
    if not m:
        raise RegistryError(f"bad version {v!r}")
    return tuple(int(x) for x in m.groups())


def hash_files(files: dict[str, bytes]) -> dict[str, str]:
    return {n: hashlib.sha256(b).hexdigest() for n, b in sorted(files.items())}


def bundle_hash(manifest: dict[str, str]) -> str:
    return hashlib.sha256(F.canonical("srnet-typed/bundle/v1", manifest)).hexdigest()


def read_dir(d) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(Path(d).iterdir())
            if p.is_file() and FILE_RE.match(p.name)}


def key_id(pub_hex: str) -> str:
    return "ed25519:" + pub_hex[:16]


def sign_entry(reg_key, template, ver, types, files: dict[str, bytes], summary="",
               source_commit="", valid_days=180, now=None) -> dict:
    now = int(time.time()) if now is None else now
    man = hash_files(files)
    e = {"template": template, "ver": ver, "types": sorted(types), "files": man,
         "bundle_sha256": bundle_hash(man), "source_commit": source_commit, "summary": summary,
         "issued_at": now, "expires_at": now + valid_days * 86400,
         "key_id": key_id(F.pub_hex(reg_key))}
    return {"entry": e, "sig": reg_key.sign(F.canonical(ENTRY_DOMAIN, e)).hex()}


def sign_withdraw(reg_key, seq, withdrawn, now=None) -> dict:
    w = {"seq": seq, "issued_at": int(time.time()) if now is None else now,
         "withdrawn": withdrawn, "key_id": key_id(F.pub_hex(reg_key))}
    return {"withdraw": w, "sig": reg_key.sign(F.canonical(WITHDRAW_DOMAIN, w)).hex()}


def verify_entry(env, pin, now=None) -> dict:
    e = env.get("entry") if isinstance(env, dict) else None
    if not isinstance(e, dict):
        raise RegistryError("no entry")
    if not F._verify(pin, env.get("sig"), F.canonical(ENTRY_DOMAIN, e)):
        raise RegistryError("entry not signed by the pinned registry key")
    now = time.time() if now is None else now
    if now > e.get("expires_at", 0):
        raise RegistryError("entry expired")
    if e.get("issued_at", 0) > now + 600:
        raise RegistryError("entry from the future")
    if not TEMPLATE_RE.match(e.get("template", "")):
        raise RegistryError("bad template name")
    ver_t(e.get("ver"))
    if not all(FILE_RE.match(n) for n in e.get("files", {})) or "index.html" not in e["files"]:
        raise RegistryError("bad file list")
    if bundle_hash(e["files"]) != e.get("bundle_sha256"):
        raise RegistryError("bundle hash mismatch")
    return e


def verify_withdraw(env, pin) -> dict:
    w = env.get("withdraw") if isinstance(env, dict) else None
    if not isinstance(w, dict) or not F._verify(pin, env.get("sig"),
                                                F.canonical(WITHDRAW_DOMAIN, w)):
        raise RegistryError("withdraw list not signed by the pinned registry key")
    return w


class TemplateManager:
    def __init__(self, data_dir, pin: str, bundled_dir, bundled_entries: list[dict], clock=time.time):
        self.dir = Path(data_dir) / "templates"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pin, self.bundled_dir, self.clock = pin, Path(bundled_dir), clock
        self.state_p = self.dir / "state.json"
        self.state = {"installed": {}, "known": {}, "withdraw_seq": 0, "withdrawn": [],
                      "pending": {}}
        if self.state_p.exists():
            self.state.update(json.loads(self.state_p.read_text()))
        for env in bundled_entries:  # shipped with the client
            e = verify_entry(env, pin, now=self.clock())
            self.state["known"].setdefault(e["template"], {}).setdefault(e["ver"], env)
            self.state["installed"].setdefault(e["template"], e["ver"])
        self._save()

    def _save(self):
        tmp = self.state_p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_p)

    def _files_dir(self, name, ver):
        d = self.dir / name / ver
        return d if d.exists() else self.bundled_dir / name

    def withdrawn(self, name, ver) -> bool:
        return any(w["template"] == name and w["ver"] == ver for w in self.state["withdrawn"])

    def _try_load(self, name, ver):
        env = self.state["known"].get(name, {}).get(ver)
        if not env or self.withdrawn(name, ver):
            return None
        try:
            e = verify_entry(env, self.pin, now=self.clock())
        except RegistryError:
            return None
        files = read_dir(self._files_dir(name, ver))
        files = {n: files.get(n, b"") for n in e["files"]}
        if hash_files(files) != e["files"]:
            return None  # tampered or missing on disk: never run it
        return {"entry": e, "files": files}

    def load(self, name):
        """Active template: the installed version if good, else the newest good fallback."""
        inst = self.state["installed"].get(name)
        t = self._try_load(name, inst) if inst else None
        if t:
            return t
        for v in sorted(self.state["known"].get(name, {}), key=ver_t, reverse=True):
            t = self._try_load(name, v)
            if t:
                return dict(t, fallback=True)
        return None

    def offer(self, env, files: dict[str, bytes]) -> dict:
        e = verify_entry(env, self.pin, now=self.clock())
        if hash_files({n: files.get(n, b"") for n in e["files"]}) != e["files"]:
            raise RegistryError("offered files do not match the signed entry")
        if self.withdrawn(e["template"], e["ver"]):
            raise RegistryError("that version is withdrawn")
        inst = self.state["installed"].get(e["template"])
        if inst and ver_t(e["ver"]) <= ver_t(inst) and not self.withdrawn(e["template"], inst):
            raise RegistryError(f"rollback refused: {e['ver']} <= installed {inst}")
        d = self.dir / e["template"] / e["ver"]
        d.mkdir(parents=True, exist_ok=True)
        for n in e["files"]:
            (d / n).write_bytes(files[n])
        self.state["pending"][e["template"]] = env
        self._save()
        return {"template": e["template"], "ver": e["ver"], "summary": e["summary"],
                "types": e["types"], "installed": inst, "needs_approval": True}

    def approve(self, name, ver) -> dict:
        env = self.state["pending"].get(name)
        if not env or env["entry"]["ver"] != ver:
            raise RegistryError("nothing pending for that template/version")
        self.state["known"].setdefault(name, {})[ver] = env
        self.state["installed"][name] = ver
        del self.state["pending"][name]
        self._save()
        return {"ok": True, "installed": ver}

    def reject(self, name):
        env = self.state["pending"].pop(name, None)
        if env:
            shutil.rmtree(self.dir / name / env["entry"]["ver"], ignore_errors=True)
        self._save()

    def apply_withdraw(self, env) -> dict:
        w = verify_withdraw(env, self.pin)
        if w["seq"] <= self.state["withdraw_seq"]:
            raise RegistryError("stale withdraw list (rollback)")
        self.state["withdraw_seq"], self.state["withdrawn"] = w["seq"], w["withdrawn"]
        self._save()
        return {"ok": True, "withdrawn": w["withdrawn"]}

    def status(self):
        out = {}
        for name in self.state["known"]:
            t = self.load(name)
            out[name] = {"installed": self.state["installed"].get(name),
                         "active": t and t["entry"]["ver"], "fallback": bool(t and t.get("fallback")),
                         "pending": name in self.state["pending"]}
        return out
