"""Typed-SNApp client core.

The core is the only trusted part of the client.  It
  * pins collection root keys and the template registry key,
  * syncs and verifies every record (typed/sync.py) and computes views (typed/views.py),
  * runs each template in a sandboxed iframe with no network, sending it only verified views
    by postMessage (via the trusted shell page),
  * accepts *drafts* from templates, validates them, shows them in its own confirmation sheet,
    and only then signs (with the author key the template never sees) and submits,
  * shows payment targets in its own payment sheet (testnet only; never sends anything),
  * serves plain-HTML .sesh pages only through the locked-down viewer (typed/htmlview.py).

Run:  python3 -m typed.core --config client.json --data DIR [--port 48920]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import mbfmt as F
from . import joins as J
from . import adapters, filecrypt as FC, htmlview, payinfo as PI, views
from . import mail as M
from .record import KIND_TYPES, TESTNET_CHAINS, codec_for, make_record
from .registry import RegistryError, TemplateManager
from . import mail as MAIL
from .mailclient import MailClient
from .store import Store
from .sync import SyncError, http_json, submit, sync_once

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
# Add-on tabs (e.g. Exits from srnet-exits): `srnet ui` starts each add-on's backend
# (token-protected, loopback) and passes them here as SRNET_ADDONS, a JSON list of
# {"name", "title", "url": "http://127.0.0.1:<port>/#<token>", "slow": [...]}. The shell reaches them
# only through the X-Core-gated /api/addon/<name>/* bridge below, so tokens never reach a page; the
# tab script is served from this origin at /addon/<name>/tab.js.
ADDON_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ADDON_CALL_RE = re.compile(r"^[a-z][a-z0-9/_-]{0,63}$")
ADDON_GET_RE = re.compile(r"^(tab\.js|tab\.css|qr\.svg\?q=[0-9a-f]{16})$")


def addons_from_env():
    try:
        raw = json.loads(os.environ.get("SRNET_ADDONS") or "[]")
    except ValueError:
        return {}
    out = {}
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict) or not ADDON_NAME_RE.match(str(a.get("name", ""))):
            continue
        m = re.fullmatch(r"(http://127\.0\.0\.1:\d+)/#([A-Za-z0-9_-]{16,})", str(a.get("url", "")))
        if m:
            out[a["name"]] = {"title": str(a.get("title") or a["name"])[:40], "base": m.group(1),
                              "tok": m.group(2), "slow": set(a.get("slow") or [])}
    return out
SHELL = HERE / "shell"
KIND_TEMPLATE = {"board": "board", "room": "room", "wiki": "wiki", "shop": "shop",
                 "drop": "drop", "id": "profile", "card": "card",
                 "directory": "directory"}
SHELL_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
             "frame-src 'self'; img-src blob:; base-uri 'none'; form-action 'none'; "
             "frame-ancestors 'none'")
ACTIONS = {"follow", "unfollow", "mute", "unmute", "reveal", "unreveal"}


def _b64(b):
    return base64.b64encode(b).decode()


class Coll:
    def __init__(self, core, cfg):
        self.name, self.kind = cfg["name"], cfg["kind"]
        self.id = cfg.get("id") or self.name  # local id (unique per client); name is the protocol name
        self.cfg, self.core = cfg, core
        if self.kind not in KIND_TEMPLATE:  # e.g. the old built-in "exit" board: now in the exits add-on
            raise ValueError(f"unknown collection kind {self.kind!r}")
        self.hosts = list(cfg.get("hosts", []))
        self.policy, self.last, self.view_cache = {}, {"host": None, "error": None}, None
        if self.kind == "card":
            self.store = None
            return
        self.root_pin = cfg["root_pin"]
        self.codec = codec_for(self.kind)
        d = Path(core.dir) / "c" / self.id
        d.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(d / "cache.sqlite3"))
        self.store.on_redact = self._on_redact
        self.pin = F.CheckpointPin(*self.store.head())

    def _on_redact(self, pid, env, tomb):
        """A host remove of a file also deletes our cached copy of its bytes."""
        if tomb.get("by") != "remove" or not isinstance(env, dict):
            return
        from .host import env_body
        sha = env_body(env).get("sha256")
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
            p = Path(self.core.dir) / "blobs" / sha
            if p.exists():
                p.unlink()

    def sync(self):
        if self.kind == "card":
            try:
                src = self.cfg.get("url")
                card = json.loads(urllib.request.urlopen(src, timeout=8).read(1 << 16)) if src \
                    else json.loads(Path(self.cfg["path"]).read_text())
                self.view_cache = adapters.card_view(card, self.cfg.get("expect_pubkey"))
                self.last = {"host": src or "file", "error": None}
            except Exception as e:
                self.last = {"host": None, "error": f"{type(e).__name__}: {e}"}
            return self.last
        if self.last.get("root_mismatch"):
            return self.last  # blocked until the user explicitly re-trusts
        order = sorted(self.hosts, key=lambda h: h != self.last.get("host"))
        try:
            r = sync_once(self.store, order, self.root_pin, self.name, self.pin, codec=self.codec)
            self.policy = r.get("policy") or {}
            self.last = {"host": r["host"], "error": None, "added": r["added"]}
        except SyncError as e:
            self.last = dict(self.last, error=str(e))
            self._check_root()
        return self.last

    def _check_root(self):
        """After a failed sync, see whether the host now names a different root key."""
        for h in self.hosts:
            try:
                code, env = http_json(h + "/v1/delegation")
                d = (env or {}).get("delegation") or {}
                r = d.get("root")
                if code == 200 and d.get("board") == self.name and isinstance(r, str) \
                        and len(r) == 64 and r != self.root_pin:
                    F.verify_delegation(env, r, self.name)  # genuinely signed by the new key
                    self.last = dict(self.last, root_mismatch={"pinned": self.root_pin, "offered": r},
                                     error="root key changed: blocked")
                    return
            except Exception:
                continue


class Core:
    def __init__(self, data_dir, config: dict, autoconfirm=False, clock=time.time):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.clock, self.autoconfirm = clock, autoconfirm
        self.addons = addons_from_env()
        kp = self.dir / "author.key"  # posting key: NOT the Session Router transport key
        if not kp.exists():
            with open(os.open(kp, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
                f.write(os.urandom(32))
        self.key = F.key_from_seed(kp.read_bytes())
        self.me = F.pub_hex(self.key)
        self.xkey = FC.x25519_from_seed(kp.read_bytes())  # file-key wrapping; never leaves the core
        self.enc_pub = FC.pub_hex(self.xkey)
        self.prefs_p = self.dir / "prefs.json"
        self.prefs = {"follows": [], "mutes": [], "moderation": "honour", "revealed": [],
                      "outbox": []}
        if self.prefs_p.exists():
            self.prefs.update(json.loads(self.prefs_p.read_text()))
        reg = config.get("registry") or json.loads((TEMPLATES / "registry.json").read_text())
        self.templates = TemplateManager(self.dir, reg["pin"], TEMPLATES, reg["entries"], clock)
        self.lock = threading.RLock()
        self._load_joined(config.get("collections", []))
        self.sites = config.get("html_sites", {})
        self.drafts = {}
        self.config, self.pending_pay = config, {}
        self.blob_cache = self.dir / "blobs"
        self.blob_cache.mkdir(exist_ok=True)
        # mail: same identity as posting (author.key); sealed store under <data>/mail
        self.mail = MailClient(self.dir / "mail", kp.read_bytes(),
                               (config.get("mail") or {}).get("hosts", []), clock=clock,
                               attacher=self.mail_attach,
                               contacts=(config.get("mail") or {}).get("contacts", []))

    def save(self):
        with self.lock:
            tmp = self.prefs_p.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(self.prefs))
            os.replace(tmp, self.prefs_p)

    # ---- sync ----
    def sync(self, name=None):
        out = {}
        for c in ([self.colls[name]] if name else self.colls.values()):
            out[c.id] = c.sync()
        self.save()
        if self.prefs["outbox"]:
            self.flush_outbox()
        return out

    # ---- joined collections (persisted; config entries are pre-joined) ----
    def _load_joined(self, cfg_colls):
        P = self.prefs
        J, T = P.setdefault("joined", {}), P.setdefault("trust", {})
        left = P.setdefault("left", [])
        order = P.setdefault("order", [])
        for c in cfg_colls:
            cid = c.get("id") or c["name"]
            if cid in left:
                continue
            ent = J.get(cid)
            if ent is None or ent.get("source") == "config":
                old = ent or {}
                J[cid] = dict({"label": c.get("label") or cid, "muted": False, "pinned": False, "read_seq": 0},
                              **{k: old[k] for k in ("label", "muted", "pinned", "read_seq") if k in old},
                              id=cid, source="config", cfg=dict(c, id=cid))
                if c.get("root_pin"):
                    # pre-joined: the config's pin is the trusted root unless the user re-trusted
                    pin = old.get("cfg", {}).get("root_pin") if old.get("retrusted") else None
                    J[cid]["cfg"]["root_pin"] = pin or c["root_pin"]
                    J[cid]["retrusted"] = bool(pin)
            if cid not in order:
                order.append(cid)
        self.colls = {}
        for cid in [i for i in order if i in J] + [i for i in J if i not in order]:
            ent = J[cid]
            try:
                self.colls[cid] = Coll(self, ent["cfg"])
            except Exception:
                continue
            if ent["cfg"].get("root_pin"):
                T.setdefault(self._trust_key(ent["cfg"]), ent["cfg"]["root_pin"])
        P["order"] = list(self.colls)

    @staticmethod
    def _host_of(cfg):
        h = (cfg.get("hosts") or [cfg.get("url") or "local"])[0]
        return urlparse(h).netloc or h

    def _trust_key(self, cfg):
        return f"{self._host_of(cfg)}/{cfg['kind']}/{cfg['name']}"

    def _new_id(self, name, host):
        cid, i = name, 2
        while cid in self.colls or cid in self.prefs["joined"]:
            cid, i = f"{name}-{i}", i + 1
        return cid

    def join_preview(self, link):
        """Parse a pasted link or .sesh address, fetch the host's signed info. Nothing is saved."""
        l = J.parse(link)
        if not l["kind"]:
            ch = []
            for o in J.offered(l["host"]):
                try:
                    pr = J.probe(l["host"], o["kind"], o["name"])
                    ch.append({"link": J.make(l["host"], o["kind"], o["name"], pr["root"]),
                               "kind": o["kind"], "name": o["name"], "root": pr["root"]})
                except J.LinkError:
                    continue
            return {"host": l["host"], "choices": ch}
        pr = J.probe(l["host"], l["kind"], l["name"])
        if l["root"] and l["root"] != pr["root"]:
            raise J.LinkError("the host's root key doesn't match the key in this link: not joining")
        tk = f"{l['host']}/{l['kind']}/{l['name']}"
        trusted = self.prefs["trust"].get(tk)
        if l["kind"] == "mail":
            joined = ["mail"] if pr["url"] in self.mail.st["hosts"] else []
        else:
            joined = [c.id for c in self.colls.values()
                      if self._trust_key(c.cfg) == tk]
        out = {"host": l["host"], "kind": l["kind"], "name": l["name"], "root": pr["root"],
               "url": pr["url"], "rules": pr["rules"], "pinned_by_link": bool(l["root"]),
               "first_seen": trusted is None, "already_joined": joined[:1],
               "link": J.make(l["host"], l["kind"], l["name"], pr["root"])}
        if trusted and trusted != pr["root"]:
            out["root_mismatch"] = {"pinned": trusted, "offered": pr["root"]}
        return out

    def join(self, link, label=None):
        with self.lock:
            pv = self.join_preview(link)
            if "choices" in pv:
                raise J.LinkError("pick one collection from this host")
            if pv.get("root_mismatch"):
                return dict(pv, joined=False, error="root key changed since you first trusted this "
                            "collection: blocked. Review it and use re-trust if you expect the change.")
            if pv["already_joined"]:
                return dict(pv, joined=True, id=pv["already_joined"][0])
            if pv["kind"] == "mail":
                return self._join_mail_host(pv)
            cid = self._new_id(pv["name"], pv["host"])
            cfg = {"id": cid, "name": pv["name"], "kind": pv["kind"], "hosts": [pv["url"]],
                   "root_pin": pv["root"]}
            self.prefs["trust"].setdefault(self._trust_key(cfg), pv["root"])  # TOFU
            lab = (label or pv["name"]).strip()[:40] or pv["name"]
            self.prefs["joined"][cid] = {"id": cid, "source": "link", "cfg": cfg, "label": lab,
                                         "muted": False, "pinned": False, "read_seq": 0}
            self.prefs["left"] = [x for x in self.prefs["left"] if x != cid]
            self.colls[cid] = Coll(self, cfg)
            self.prefs["order"] = list(self.colls)
            self.save()
        self.sync(cid)
        return dict(pv, joined=True, id=cid)

    def _join_mail_host(self, pv):
        """Directory/link Join for a mail host: add it to your mail hosts and register an inbox."""
        hosts = list(self.mail.st["hosts"])
        if len(hosts) >= M.MAX_HOSTS:
            return dict(pv, joined=False, error=f"you already use {M.MAX_HOSTS} mail hosts: "
                        "remove one on the Mail page first")
        self.prefs["trust"].setdefault(f"{pv['host']}/mail/{pv['name']}", pv["root"])
        self.mail.set_hosts(hosts + [pv["url"]])
        self.save()
        try:
            reg = self.mail.register()
        except Exception as e:  # host added; registration retried on the next Check mail
            reg = {"error": str(e)}
        return dict(pv, joined=True, id="mail", mail=True, register=reg)

    RETRUST_PHRASE = "trust the new key"

    def retrust(self, cid, offered, confirm):
        """Explicitly replace a pinned root key. Wipes that collection's cache."""
        c = self.colls[cid]
        if confirm != self.RETRUST_PHRASE:
            raise ValueError(f"type '{self.RETRUST_PHRASE}' to confirm")
        mm = c.last.get("root_mismatch") or {}
        if offered != mm.get("offered"):
            pv = self.join_preview(J.make(self._host_of(c.cfg), c.kind, c.name, offered))
            if pv["root"] != offered:
                raise ValueError("host isn't offering that key")
        with self.lock:
            ent = self.prefs["joined"][cid]
            ent["cfg"] = dict(ent["cfg"], root_pin=offered)
            ent["retrusted"], ent["read_seq"] = True, 0
            self.prefs["trust"][self._trust_key(ent["cfg"])] = offered
            c.store.db.close()
            d = self.dir / "c" / cid
            shutil.rmtree(d, ignore_errors=True)
            self.colls[cid] = Coll(self, ent["cfg"])
            self.save()
        self.sync(cid)
        return {"id": cid, "root": offered}

    def leave(self, cid):
        with self.lock:
            c = self.colls.pop(cid)
            ent = self.prefs["joined"].pop(cid, {})
            if ent.get("source") == "config":
                self.prefs["left"].append(cid)
            self.prefs["order"] = list(self.colls)
            if c.store is not None:
                c.store.db.close()
            shutil.rmtree(self.dir / "c" / cid, ignore_errors=True)
            self.save()
        return {"left": cid}

    def update_joined(self, cid, label=None, muted=None, pinned=None):
        ent = self.prefs["joined"][cid]
        if label is not None:
            lab = re.sub(r"[\x00-\x1f\x7f]", "", str(label)).strip()[:40]
            ent["label"] = lab or self.colls[cid].name
        if muted is not None:
            ent["muted"] = bool(muted)
        if pinned is not None:
            ent["pinned"] = bool(pinned)
        self.save()
        return self.sidebar()

    def share(self, cid):
        c = self.colls[cid]
        if c.kind == "card":
            raise ValueError("built-in views can't be shared")
        return {"link": J.make(self._host_of(c.cfg), c.kind, c.name, c.root_pin)}

    def unread(self, cid):
        c, ent = self.colls[cid], self.prefs["joined"].get(cid, {})
        if c.store is None or ent.get("muted"):
            return 0
        items, _ = self._items(c)
        mark = ent.get("read_seq", 0)
        return sum(1 for it in items if it["seq"] > mark and it["author"] != self.me
                   and not it["t"].endswith(".profile"))

    def mark_read(self, cid):
        c, ent = self.colls[cid], self.prefs["joined"].get(cid)
        if ent is not None and c.store is not None:
            items, _ = self._items(c)
            new = max([it["seq"] for it in items] + [ent.get("read_seq", 0)])
            if new != ent.get("read_seq"):
                ent["read_seq"] = new
                self.save()

    def sidebar(self):
        groups = {}
        for cid, c in self.colls.items():
            ent = self.prefs["joined"].get(cid, {})
            groups.setdefault(self._host_of(c.cfg), []).append({
                "id": cid, "label": ent.get("label") or cid, "name": c.name, "kind": c.kind,
                "template": KIND_TEMPLATE[c.kind], "unread": self.unread(cid),
                "muted": bool(ent.get("muted")), "pinned": bool(ent.get("pinned")),
                "source": ent.get("source", "config"), "error": c.last.get("error"),
                "root_mismatch": c.last.get("root_mismatch")})
        out = []
        for host, items in groups.items():
            items.sort(key=lambda x: (not x["pinned"], x["label"].lower()))
            out.append({"host": host, "short": host if len(host) <= 24 else host[:10] + "…" + host[-10:],
                        "items": items})
        out.sort(key=lambda g: (not any(i["pinned"] for i in g["items"]), g["host"]))
        return {"groups": out}

    # ---- moderation filter (host-signed records, user override) ----
    def _hidden(self, c):
        hp, ha = {}, {}
        for r in c.store.records_since(0, 1 << 30):
            if r["kind"] == "mod":
                m = r["rec"]["mod"]
                (hp if m["target_type"] == "post" else ha)[m["target"]] = m["action"] == "hide"
        return {k for k, v in hp.items() if v}, {k for k, v in ha.items() if v}

    def _items(self, c):
        items = views.items_from_store(c.store, c.codec)
        hp, ha = self._hidden(c)
        mutes = set(self.prefs["mutes"])
        out, hidden = [], 0
        for it in items:
            if it["t"] == "tombstone":
                out.append(it)
                continue
            if it["author"] in mutes:
                continue
            if self.prefs["moderation"] != "ignore" and it["id"] not in self.prefs["revealed"] \
                    and (it["id"] in hp or it["author"] in ha):
                hidden += 1
                continue
            out.append(it)
        return out, hidden

    def view(self, name, read=False):
        c = self.colls[name]
        if read:
            self.mark_read(name)
        base = {"collection": name, "kind": c.kind, "me": self.me[:6], "me_key": self.me,
                "status": c.last, "rules": (c.policy or {}).get("rules", ""),
                "template": KIND_TEMPLATE[c.kind]}
        if c.kind == "card":
            return dict(base, view=c.view_cache or {}, hidden=0)
        items, hidden = self._items(c)
        if c.kind == "board":
            ids = views.identities(items)
            posts = [it for it in items if it["t"] == "board.post"]
            replies = {}
            for p in posts:
                if p["body"]["reply_to"]:
                    replies[p["body"]["reply_to"]] = replies.get(p["body"]["reply_to"], 0) + 1
            v = {"posts": [{"id": p["id"], "seq": p["seq"], "ts": p["ts"], "author": p["author"],
                            "who": views.label(ids, p["author"]), "text": p["body"]["text"],
                            "reply_to": p["body"]["reply_to"], "replies": replies.get(p["id"], 0),
                            "mine": p["author"] == self.me,
                            "followed": p["author"] in self.prefs["follows"]}
                           for p in reversed(posts)]}
        elif c.kind == "id":
            ids = views.identities(items)
            v = {"people": [{"key": k, "label": ids["labels"][k], "name": p.get("name"),
                             "bio": p.get("bio") or "", "current": ids["current"].get(k, k),
                             "recovery_committed": bool(p.get("next_key_hash")),
                             "rotation": ids["rotations"].get(k)}
                            for k, p in ids["profiles"].items()],
                 "rotations": [{"from": k, **r} for k, r in ids["rotations"].items()]}
        else:
            v = (views.drop_view(items, c.policy or {}, self.me, self.enc_pub) if c.kind == "drop"
                 else views.VIEWS[c.kind](items, c.policy or {}))
        # host removals: shown as "Removed by host: <reason>"; never revealable
        removed = [{"id": it["id"], "seq": it["seq"], "reason": it.get("reason", "")}
                   for it in items if it["t"] == "tombstone" and it.get("by") == "remove"]
        extra = {}
        if c.kind in ("room", "board"):
            from .host import active_pins
            pol = c.policy or {}
            extra["header"] = {"description": pol.get("description") or "",
                               "rules": pol.get("rules") or ""}
            pins = active_pins(c.store.records_since(0, 1 << 30))
            rows = v.get("lines") or v.get("posts") or []
            extra["pinned"] = [r for p in pins for r in rows if r["id"] == p]
        return dict(base, view=v, hidden=hidden, removed=removed, **extra)

    # ---- drafts: template proposes, core validates, user confirms, core signs ----
    def draft(self, name, t, body, refs):
        c = self.colls.get(name)
        if not c or c.kind == "card":
            raise F.FormatError("read-only collection")
        if not isinstance(body, dict) or not isinstance(refs, list):
            raise F.FormatError("bad draft")
        v = 1
        if c.kind == "board":
            if t != "board.post":
                raise F.FormatError("board only accepts board.post")
            rt = body.get("reply_to") or (refs[0] if refs else None)
            F.make_post(self.key, name, str(body.get("text", "")), rt)  # validation only
            body = {"text": body.get("text"), "reply_to": rt}
        else:
            spec = KIND_TYPES[c.kind].latest(t)
            v = spec.v
            spec.check_body({k: v for k, v in body.items() if v is not None}, refs)
            if t == "id.profile" and not body.get("enc_key"):
                body = dict(body, enc_key=self.enc_pub)  # lets others share encrypted files with you
        tok = secrets.token_hex(12)
        with self.lock:
            self.drafts[tok] = {"collection": name, "t": t, "body": body, "refs": refs,
                                "v": v, "at": self.clock()}
        prev = {"token": tok, "collection": name, "t": t,
                "lines": [_line(k, v) for k, v in body.items() if v not in (None, "")],
                "refs": refs}
        if self.autoconfirm:
            return dict(prev, result=self.confirm(tok))
        return prev

    def _pow(self, c):
        pol = c.policy or {}
        mine = c.store.db.execute("SELECT MIN(ts) FROM log WHERE author=?", (self.me,)).fetchone()[0]
        new = mine is None or self.clock() - mine < pol.get("new_key_age_s", 86400)
        return pol.get("new_key_pow_bits" if new else "pow_bits")

    def _sign_submit(self, d):
        c = self.colls[d["collection"]]
        if not c.policy:
            c.sync()
        bits = self._pow(c)
        if bits is None:
            return {"ok": False, "unreachable": True, "error": "no host reachable"}
        if c.kind == "board":
            env = F.make_post(self.key, c.name, d["body"]["text"], d["body"]["reply_to"],
                              pow_bits=bits)
        else:
            env = make_record(self.key, KIND_TYPES[c.kind], c.name, d["t"], d["body"], d["refs"],
                              v=d.get("v", 1), pow_bits=bits)
        r = submit(c.hosts, env)
        if r.get("ok"):
            c.sync()
        return r

    def confirm(self, tok):
        with self.lock:
            d = self.drafts.pop(tok, None)
        if not d:
            return {"ok": False, "error": "unknown or already used draft"}
        r = self._sign_submit(d)
        if r.get("unreachable"):
            self.prefs["outbox"].append(d)
            self.save()
            r = dict(r, queued=True)
        return r

    def cancel(self, tok):
        with self.lock:
            self.drafts.pop(tok, None)
        return {"ok": True}

    def flush_outbox(self):
        keep = []
        for d in self.prefs["outbox"]:
            r = self._sign_submit(d)
            if r.get("unreachable"):
                keep.append(d)
        self.prefs["outbox"] = keep
        self.save()

    def delete(self, name, rid):
        c = self.colls[name]
        if c.store.author_of(rid) != self.me:
            return {"ok": False, "error": "not your record"}
        r = submit(c.hosts, F.make_delete(self.key, name, [rid]), "/v1/delete")
        if r.get("ok"):
            c.sync()
        return r

    def action(self, name, args):
        if name not in ACTIONS:
            raise F.FormatError("unknown action")
        target = str(args.get("target", ""))
        kind = {"follow": "follows", "unfollow": "follows", "mute": "mutes", "unmute": "mutes",
                "reveal": "revealed", "unreveal": "revealed"}[name]
        if kind != "revealed":
            F.load_pub(target)
        s = set(self.prefs[kind])
        (s.discard if name.startswith("un") else s.add)(target)
        self.prefs[kind] = sorted(s)
        self.save()
        return {"ok": True}

    # ---- payments: the core shows address, amount and QR; it never sends or checks a payment ----
    def _listing(self, name, listing_id):
        c = self.colls[name]
        if c.kind != "shop":
            raise F.FormatError("not a store")
        l = next((x for x in views.shop_view(self._items(c)[0], c.policy or {})["listings"]
                  if x["id"] == listing_id), None)
        if not l or not l["active"]:
            raise F.FormatError("no such active listing")
        return l

    def pay_draft(self, name, listing_id):
        l = self._listing(name, listing_id)
        if not l.get("payable"):
            raise F.FormatError("this listing can't be paid: " + (l.get("note") or "seller needs to update"))
        cid, to = l["chain_id"], l["pay_to"]
        if l["currency"] == "USDC":
            units = PI.order_units(PI.usdc_units(l["price"]))   # this order's own exact amount
            cap = PI.max_units(self.config)
            if units > cap:
                raise F.FormatError(f"{PI.fmt_usdc(units)} USDC is over your limit of {PI.fmt_usdc(cap)} per "
                                    "payment (\"payments\": {\"max_usdc\": ...} in client.json)")
            uri, amount, token = PI.usdc_link(cid, to, units), PI.fmt_usdc(units), PI.USDC[cid]
        else:  # ETH: testnet only
            if cid not in TESTNET_CHAINS:
                raise F.FormatError("ETH payments are testnet-only")
            uri, amount, token = PI.eth_link(cid, to, PI.eth_wei(l["price"])), l["price"], None
        tok = secrets.token_hex(12)
        with self.lock:
            self.pending_pay = {k: v for k, v in self.pending_pay.items() if self.clock() - v["at"] < 3600}
            self.pending_pay[tok] = {"collection": name, "listing": l["id"], "chain_id": cid, "uri": uri,
                                     "amount": amount if l["currency"] == "USDC" else None, "at": self.clock()}
        return {"ok": True, "token": tok, "qr": PI.have_qr(), "sheet": {
            "network": PI.label(cid), "chain_id": cid, "to": to, "amount": amount, "currency": l["currency"],
            "token_address": token, "uri": uri, "item": l["title"], "seller": l["seller_label"],
            "listing": l["id"], "testnet": cid in PI.TESTNETS,
            "note": "Pay from your own wallet: send exactly this amount on " + PI.label(cid)
                    + ". You pay the network fee. Payments are final. This app never sends funds."}}

    def pay_qr(self, tok):
        d = self.pending_pay.get(tok)
        if not d:
            return None
        return PI.qr_svg(d["uri"])

    def pay_order(self, tok, tx, note=None):
        """After paying: draft a shop.order naming the transaction, for the seller (shown in a sheet)."""
        d = self.pending_pay.get(tok)
        if not d:
            raise F.FormatError("unknown or expired payment")
        tx = str(tx or "").strip().lower()
        if not PI.TX_RE.match(tx):
            raise F.FormatError("paste the transaction hash (0x followed by 64 hex characters)")
        body = {"chain_id": d["chain_id"], "tx": tx, "note": (note or None)}
        if d.get("amount"):
            body["amount"] = d["amount"]
        return self.draft(d["collection"], "shop.order", body, [d["listing"]])

    def verify_order(self, name, order_id):
        """Seller: was this order really paid? The core doesn't check payments; the exits add-on can
        (read-only). Without it, return the explorer link.

        Orders are public, so a buyer could copy someone else's transaction hash. An order only
        counts if it names the listing's chain, the exact amount it sent matches the transaction,
        and no other order names the same transaction (if one does, both are shown as in dispute)."""
        c = self.colls[name]
        listings = views.shop_view(self._items(c)[0], c.policy or {})["listings"]
        for l in listings:
            for o in l["orders"]:
                if o["id"] != order_id:
                    continue
                if "tx" not in o:
                    return {"ok": True, "checked": False, "note": "an old SRNet-balance order; it can't be checked here"}
                link = PI.tx_link(o["chain_id"], o["tx"])
                if o["chain_id"] != l.get("chain_id"):
                    return {"ok": True, "checked": True, "paid": False, "link": link,
                            "why": f"this order names {PI.label(o['chain_id'])}, but the listing takes payment on "
                                   f"{PI.label(l.get('chain_id'))}"}
                same = sorted((x for ll in listings for x in ll["orders"]
                               if x.get("tx", "").lower() == o["tx"].lower() and x.get("chain_id") == o["chain_id"]
                               and x["id"] != o["id"]), key=lambda x: (x["ts"], x["id"]))
                if same:
                    first = min([o] + same, key=lambda x: (x["ts"], x["id"]))
                    why = (f"that transaction is already named by order {first['id']} (earlier)" if first is not o
                           else "that transaction is also named by order(s) " + ", ".join(x["id"] for x in same)
                           + "; ask the buyers which of them paid (compare the from address)")
                    return {"ok": True, "checked": True, "paid": False, "link": link, "why": why}
                if "exits" in self.addons and l.get("currency") == "USDC":
                    amount = l["price"]
                    if o.get("amount"):
                        try:
                            ok = PI.order_amount_ok(PI.usdc_units(l["price"]), PI.usdc_units(o["amount"]))
                        except PI.PayError:
                            ok = False
                        if not ok:
                            return {"ok": True, "checked": True, "paid": False, "link": link,
                                    "why": f"the order says {o['amount']} USDC, which isn't this listing's price "
                                           f"{l['price']} plus an order id"}
                        amount = o["amount"]
                    r = self.addon_call("exits", "verify", json.dumps(
                        {"chain_id": o["chain_id"], "tx": o["tx"], "to": l["pay_to"], "amount": amount}).encode())
                    try:
                        r = json.loads(r)
                    except ValueError:
                        r = {"ok": False, "err": "bad reply from the exits add-on"}
                    return dict(r, checked=bool(r.get("ok")), link=link)
                return {"ok": True, "checked": False, "link": link,
                        "note": "Open the link to check it, or install the exits add-on (srnet-exits) to check here."
                                + (f" It should pay exactly {o['amount']} USDC." if o.get("amount") else "")}
        raise F.FormatError("no such order")

    def addon_call(self, name, call, raw=None, method="POST", deadline=None):
        """Forward one call to an add-on backend. Returns the raw reply body."""
        a = self.addons.get(name)
        if a is None:
            return json.dumps({"ok": False, "err": f"the {name} add-on isn't running: start SRNet with `srnet ui`"}).encode()
        ok = (ADDON_GET_RE.match(call) if method == "GET" else ADDON_CALL_RE.match(call)) and ".." not in call
        if not ok:
            return json.dumps({"ok": False, "err": "bad add-on call"}).encode()
        if method == "GET":
            req = urllib.request.Request(f"{a['base']}/{call}{'&' if '?' in call else '?'}k={a['tok']}")
        else:
            req = urllib.request.Request(f"{a['base']}/api/{call}", data=raw or b"{}", method="POST",
                                         headers={"Content-Type": "application/json", "X-CSRF-Token": a["tok"]})
        to = deadline or (600 if call in a["slow"] else 18)

        def fetch():
            try:
                with urllib.request.urlopen(req, timeout=to) as r:
                    return r.read(1 << 22)
            except urllib.error.HTTPError as e:
                return e.read(1 << 16)
            except OSError as e:
                return json.dumps({"ok": False, "err": f"{name} add-on: " + str(e)[:200]}).encode()
        # A hard wall-clock deadline (under the shell's 20 s) so a tab never spins: urlopen's timeout
        # is per socket read, not for the whole call.
        box = []
        t = threading.Thread(target=lambda: box.append(fetch()), daemon=True)
        t.start()
        t.join(to)
        return box[0] if box else json.dumps({"ok": False, "err": "timed out"}).encode()

    # ---- file drop: encrypted by default; the host only ever sees ciphertext ----
    def max_file_bytes(self, c) -> int:
        """The drop's limit from its checkpoint-signed policy (older hosts: 4 MiB)."""
        v = (c.policy or {}).get("max_file_bytes")
        return int(v) if isinstance(v, int) and v > 0 else FC.LEGACY_MAX_FILE

    UP_CHUNK = 1 << 20

    def _put_chunked(self, c, host, data: bytes, h: str):
        """Signed, chunked, resumable upload.  Returns True, False (refused) or None (no
        /v1/upload on this host: an older host).  Re-running after an interruption asks the
        host how many bytes it already has and continues from there."""
        from .hub import sign_upload
        code, r = http_json(host + "/v1/upload", sign_upload(self.key, c.name, h, len(data)), timeout=30)
        if code == 404 and "error" in r and r.get("error") == "not found":
            return None
        if code != 200 or not r.get("ok"):
            raise FC.CryptoError(r.get("error") or f"upload refused ({code})")
        have = int(r.get("have", 0))
        step = min(int(r.get("chunk") or self.UP_CHUNK), self.UP_CHUNK)
        while not r.get("done"):
            req = urllib.request.Request(f"{host}/v1/upload/{h}?off={have}", data=data[have:have + step],
                                         headers={"Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    r = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                r = json.loads(e.read() or b"{}")
                if e.code == 409 and "have" in r:  # host is elsewhere: continue from there
                    have, r = int(r["have"]), {}
                    continue
                raise FC.CryptoError(r.get("error") or f"chunk refused ({e.code})")
            have = int(r.get("have", have))
        return True

    def _put_blob(self, c, data: bytes) -> bool:
        h = hashlib.sha256(data).hexdigest()
        refused = None
        for host in c.hosts:
            try:
                ok = self._put_chunked(c, host, data, h)
                if ok:
                    return True
            except FC.CryptoError as e:
                refused = e
                continue
            except OSError:
                continue
            try:
                req = urllib.request.Request(host + "/v1/blob", data=data,
                                             headers={"Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    if json.loads(r.read()).get("sha256") == h:
                        return True
            except OSError:
                continue
        if refused:
            raise refused
        return False

    def mail_attach(self, data: bytes, filename: str, mime: str) -> str:
        """One-click mail attachment: encrypt, store the blob on a joined file drop, return
        its key-in-fragment link. No drop record is posted, so it isn't listed publicly."""
        drops = [c for c in self.colls.values() if c.kind == "drop" and c.hosts]
        if not drops:
            raise MAIL.MailError("join a file drop first: attachments are stored there, encrypted")
        pref = (self.config.get("mail") or {}).get("attach_drop")
        drops.sort(key=lambda c: c.name != pref)
        for c in drops:
            try:
                blob, key = FC.encrypt(data, filename, mime, max_plain=self.max_file_bytes(c))
            except FC.CryptoError:
                continue
            try:
                ok = self._put_blob(c, blob)
            except FC.CryptoError:
                continue
            if ok:
                return FC.link(c.hosts[0], hashlib.sha256(blob).hexdigest(), key)
        raise MAIL.MailError("no file-drop host took the attachment")

    def _drop(self, name):
        c = self.colls[name]
        if c.kind != "drop":
            raise F.FormatError("not a drop")
        return c

    def _enc_key_of(self, c, who: str) -> str:
        """A recipient given as an X25519 enc key, or as an author key whose profile lists one."""
        if not re.fullmatch(r"[0-9a-f]{64}", who or ""):
            raise F.FormatError("recipient must be a 64-hex key")
        items, _ = self._items(c)
        prof = views.identities(items)["profiles"].get(who)
        if prof and prof.get("enc_key"):
            return prof["enc_key"]
        return who  # taken as an enc key

    def upload(self, name, data: bytes, filename: str, mime: str, note: str = "",
               mode: str = "gated", recipients=(), prev: str | None = None):
        c = self._drop(name)
        if mode not in ("gated", "link", "plain"):
            raise F.FormatError("mode must be gated, link or plain")
        lim = self.max_file_bytes(c)
        if len(data) > lim:  # refused before anything is sent
            return {"ok": False, "error": f"file too large for this drop (max {lim} bytes)",
                    "max_file_bytes": lim}
        if mode == "plain":
            if not self._put_blob(c, data):
                return {"ok": False, "error": "no host took the blob"}
            body = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mime": mime,
                    "name": filename, "note": note or None, "mode": "plain"}
            return self.draft(name, "drop.item", body, [prev] if prev else [])
        blob, key = FC.encrypt(data, filename, mime)
        sha = hashlib.sha256(blob).hexdigest()
        to = [self.enc_pub] + [k for k in (self._enc_key_of(c, r) for r in recipients) if k != self.enc_pub]
        wraps = [FC.wrap(key, k, sha) for k in dict.fromkeys(to)]
        if not self._put_blob(c, blob):
            return {"ok": False, "error": "no host took the blob"}
        (self.blob_cache / sha).write_bytes(blob)
        body = {"sha256": sha, "size": len(blob), "note": note or None, "mode": mode, "wraps": wraps}
        return self.draft(name, "drop.item", body, [prev] if prev else [])

    def _state(self, name, fid):
        c = self._drop(name)
        st = views.drop_state(self._items(c)[0]).get(fid)
        if not st:
            raise F.FormatError("no such file")
        return c, st

    def _file_key(self, st) -> bytes:
        w = st["wraps"].get(self.enc_pub)
        if not w:
            raise F.FormatError("you don't have access to this file; request it")
        return FC.unwrap(w, self.xkey, st["item"]["body"]["sha256"])

    def open_file(self, name, fid):
        """Fetch, verify by hash and (if encrypted) decrypt. Returns (bytes, filename, mime)."""
        c, st = self._state(name, fid)
        b = st["item"]["body"]
        blob = self.blob(name, b["sha256"])
        if blob is None:
            raise F.FormatError("blob unavailable")
        if st["mode"] == "plain":
            return blob, b.get("name") or "file", b.get("mime") or "application/octet-stream"
        try:
            data, meta = FC.decrypt(blob, self._file_key(st))
        except FC.CryptoError as e:
            raise F.FormatError(str(e))
        return data, meta["name"], meta["mime"]

    def share_link(self, name, fid):
        c, st = self._state(name, fid)
        if st["mode"] != "link":
            raise F.FormatError("only public-link files have a link")
        return {"ok": True, "link": FC.link(c.hosts[0], st["item"]["body"]["sha256"], self._file_key(st)),
                "note": "Anyone with this link can open the file. The key is after '#', so the host never sees it."}

    def open_link(self, url: str):
        try:
            base, sha, key = FC.parse_link(url)
            u = urlparse(base)
            if u.scheme not in ("http", "https"):
                raise FC.CryptoError("bad link")
            blob = urllib.request.urlopen(base, timeout=30).read(FC.max_cipher_len() + 1)  # fragment stripped
            if hashlib.sha256(blob).hexdigest() != sha:
                raise FC.CryptoError("file does not match its hash")
            data, meta = FC.decrypt(blob, key)
        except (FC.CryptoError, ValueError) as e:
            raise F.FormatError(str(e))
        return data, meta["name"], meta["mime"]

    def access(self, name, fid):
        """Owner's access sheet: who can open the file (labels + enc key), pending requests."""
        c, st = self._state(name, fid)
        if st["owner"] != self.me:
            raise F.FormatError("only the owner manages access")
        return {"ok": True, "mode": st["mode"],
                "recipients": [k for k in st["wraps"] if k != self.enc_pub],
                "revoked": sorted(st["revoked"]),
                "requests": [{"id": r["id"], "from": r["author"], "enc_key": r["enc_key"], "note": r["note"]}
                             for r in st["requests"] if r["enc_key"] not in st["wraps"]],
                "note": "Revoking stops future versions from being shared with that key. "
                        "It can't take back a copy that was already downloaded."}

    def grant(self, name, fid, who):
        c, st = self._state(name, fid)
        if st["owner"] != self.me:
            raise F.FormatError("only the owner can grant access")
        if st["mode"] == "plain":
            raise F.FormatError("plain files are public already")
        to = self._enc_key_of(c, who)
        sha = st["item"]["body"]["sha256"]
        return self.draft(name, "drop.grant", {"sha256": sha, "wrap": FC.wrap(self._file_key(st), to, sha)}, [fid])

    def approve(self, name, request_id):
        items, _ = self._items(self._drop(name))
        r = next((it for it in items if it["id"] == request_id and it["t"] == "drop.request"), None)
        if not r:
            raise F.FormatError("no such request")
        return self.grant(name, r["refs"][0], r["body"]["enc_key"])

    def revoke(self, name, fid, enc_key):
        c, st = self._state(name, fid)
        if st["owner"] != self.me:
            raise F.FormatError("only the owner can revoke")
        return self.draft(name, "drop.revoke", {"to": enc_key}, [fid])

    def request_access(self, name, fid, note=""):
        self._state(name, fid)
        return self.draft(name, "drop.request", {"enc_key": self.enc_pub, "note": note or None}, [fid])

    def new_version(self, name, fid, data: bytes, filename: str, mime: str, note: str = ""):
        """Re-encrypt with a fresh key; shared with current recipients minus revoked ones."""
        c, st = self._state(name, fid)
        if st["owner"] != self.me:
            raise F.FormatError("only the owner can publish a new version")
        keep = [k for k in st["wraps"] if k not in st["revoked"] and k != self.enc_pub]
        return self.upload(name, data, filename, mime, note, mode=st["mode"], recipients=keep, prev=fid)

    def blob(self, name, sha):
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise F.FormatError("bad hash")
        p = self.blob_cache / sha
        if p.exists():
            return p.read_bytes()
        for host in self.colls[name].hosts:
            try:
                b = urllib.request.urlopen(f"{host}/v1/blob/{sha}", timeout=30).read(FC.max_cipher_len() + 1)
            except OSError:
                continue
            if hashlib.sha256(b).hexdigest() == sha:  # fetched by hash and checked
                p.write_bytes(b)
                return b
        return None

    # ---- template frames ----
    def frame(self, template):
        t = self.templates.load(template)
        if not t:
            return None, None
        f = t["files"]
        scripts = [f[n].decode() for n in ("tk.js", "app.js") if n in f]
        style = f.get("app.css", b"").decode()
        html = f["index.html"].decode()
        html = html.replace("<!--STYLE-->", f"<style>{style}</style>")
        html = html.replace("<!--SCRIPTS-->", "".join(f"<script>{s}</script>" for s in scripts))

        def h(s):
            return "'sha256-" + _b64(hashlib.sha256(s.encode()).digest()) + "'"
        csp = ("default-src 'none'; script-src " + " ".join(h(s) for s in scripts) +
               "; style-src " + h(style) + "; img-src 'none'; connect-src 'none'; "
               "font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; "
               "worker-src 'none'; form-action 'none'; base-uri 'none'; "
               "frame-ancestors 'self'; sandbox allow-scripts")
        return html.encode(), csp

    def viewer(self, name_path):
        name, _, path = name_path.partition("/")
        base = self.sites.get(name)
        if not base:
            return htmlview.render("<p>No route to this .sesh name in this prototype.</p>", name)
        u = urlparse(base)
        if u.hostname not in ("127.0.0.1", "localhost", "::1") and \
                not (u.hostname or "").endswith(".sesh"):
            return htmlview.render("<p>Clearnet requests are always blocked.</p>", name)
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/" + path, timeout=10) as r:
                src = r.read(2_000_000).decode("utf-8", "replace")
        except OSError as e:
            src = f"<p>Could not load page: {type(e).__name__}</p>"
        return htmlview.render(src, name)


def make_handler(core: Core, test_mode=False):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype, csp=SHELL_CSP, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def _addon(self, name, call, raw=None, method="POST"):
            """Bridge to an add-on backend (e.g. exits). Its own rules live there."""
            body = core.addon_call(name, call, raw, method)
            if method == "GET" and call.startswith("qr.svg"):
                if body[:1] == b"<":
                    return self._send(200, body, "image/svg+xml")
                return self._json(404, {"ok": False, "err": "no QR"})
            return self._send(200, body, "application/json; charset=utf-8")

        def _api_ok(self):
            """Only the trusted shell may call the API: same loopback host, no foreign Origin,
            and the X-Core header (which navigations, forms and sandboxed frames can't add)."""
            hosthdr = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            origin = self.headers.get("Origin")
            if hosthdr not in ("127.0.0.1", "localhost", "[::1]"):
                return False
            if origin is not None and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                return False
            return self.headers.get("X-Core") == "1"

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            static = {"/": ("shell.html", "text/html; charset=utf-8"),
                      "/shell.js": ("shell.js", "text/javascript; charset=utf-8"),
                      "/shell.css": ("shell.css", "text/css; charset=utf-8"),
                      "/mail": ("mail.html", "text/html; charset=utf-8"),
                      "/mail.js": ("mail.js", "text/javascript; charset=utf-8")}
            if u.path in static:
                n, ct = static[u.path]
                return self._send(200, (SHELL / n).read_bytes(), ct)
            m = re.fullmatch(r"/addon/([a-z][a-z0-9-]{0,31})/(tab\.js|tab\.css)", u.path)
            if m:
                if m.group(1) not in core.addons:
                    return self._send(404, b"", "text/plain")
                body = core.addon_call(m.group(1), m.group(2), method="GET")
                ct = "text/javascript; charset=utf-8" if m.group(2).endswith(".js") else "text/css; charset=utf-8"
                return self._send(200, body, ct)
            if u.path.startswith("/frame/"):
                body, csp = core.frame(u.path.split("/")[2])
                if body is None:
                    return self._send(404, b"template unavailable", "text/plain", "default-src 'none'")
                return self._send(200, body, "text/html; charset=utf-8", csp)
            if u.path == "/viewer":
                page = core.viewer(q.get("name", [""])[0]).encode()
                return self._send(200, page, "text/html; charset=utf-8", htmlview.CSP)
            if not u.path.startswith("/api/") or not self._api_ok():
                return self._json(403, {"error": "core API: trusted shell only"})
            p = u.path[5:]
            m = re.fullmatch(r"addon/([a-z][a-z0-9-]{0,31})/qr\.svg", p)
            if m:
                qid = (q.get("q") or [""])[0]
                if not re.fullmatch(r"[0-9a-f]{16}", qid):
                    return self._json(400, {"error": "bad id"})
                return self._addon(m.group(1), "qr.svg?q=" + qid, method="GET")
            if p == "pay/qr.svg":
                svg = core.pay_qr((q.get("token") or [""])[0])
                if not svg:
                    return self._json(404, {"error": "no QR (install the qrcode package; the link works too)"})
                return self._send(200, svg, "image/svg+xml")
            try:
                if p == "collections":
                    return self._json(200, {"collections": [
                        {"name": c.id, "kind": c.kind, "template": KIND_TEMPLATE[c.kind],
                         "root_mismatch": c.last.get("root_mismatch"),
                         "label": core.prefs["joined"].get(c.id, {}).get("label") or c.id}
                        for c in core.colls.values()], "me": core.me,
                        "templates": core.templates.status(), "test": test_mode,
                        "outbox": len(core.prefs["outbox"]),
                        "addons": [{"name": n, "title": a["title"]} for n, a in sorted(core.addons.items())]})
                if p == "sidebar":
                    return self._json(200, core.sidebar())
                if p.startswith("view/"):
                    return self._json(200, core.view(p[5:], read=q.get("read") == ["1"]))
                if p.startswith("blob/"):
                    _, name, sha = p.split("/")
                    b = core.blob(name, sha)
                    if b is None:
                        return self._json(404, {"error": "blob unavailable"})
                    return self._send(200, b, "application/octet-stream", "default-src 'none'; sandbox",
                                      [("Content-Disposition", f'attachment; filename="{sha[:12]}.bin"')])
            except (KeyError, ValueError, F.FormatError) as e:
                return self._json(400, {"error": str(e)})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if not u.path.startswith("/api/") or not self._api_ok():
                return self._json(403, {"error": "core API: trusted shell only"})
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(n, (FC.MAX_PLAIN * 4 // 3 if u.path == "/api/mail/attach" else FC.MAX_PLAIN) + (64 << 10)))
            p = u.path[5:]
            m = re.fullmatch(r"addon/([a-z][a-z0-9-]{0,31})/([a-z][a-z0-9/_-]{0,63})", p)
            if m:
                return self._addon(m.group(1), m.group(2), raw)
            if p == "preflight":
                return self._json(200, _preflight(core.dir))
            try:
                if p == "upload":
                    q = parse_qs(u.query)
                    rec = [r for r in q.get("to", [""])[0].replace(",", " ").split() if r]
                    args = (q["c"][0], raw, q.get("name", ["file"])[0][:128],
                            q.get("mime", ["application/octet-stream"])[0], q.get("note", [""])[0])
                    if q.get("prev"):
                        return self._json(200, core.new_version(args[0], q["prev"][0], *args[1:]))
                    return self._json(200, core.upload(*args, mode=q.get("mode", ["gated"])[0],
                                                       recipients=rec))
                a = json.loads(raw or b"{}")
                if p.startswith("mail/"):
                    try:
                        return self._json(200, core.mail.api(p[5:], a))
                    except (MAIL.MailError, FC.CryptoError) as e:
                        return self._json(400, {"ok": False, "error": str(e)})
                if p in ("file/open", "file/openlink"):
                    data, fname, mime = (core.open_file(a["c"], a["id"]) if p == "file/open"
                                         else core.open_link(a["link"]))
                    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", fname)[:100] or "file"
                    return self._send(200, data, "application/octet-stream", "default-src 'none'; sandbox",
                                      [("Content-Disposition", f'attachment; filename="{safe}"'),
                                       ("X-File-Mime", re.sub(r"[^a-z0-9/.+-]", "", mime)[:80])])
                if p == "file/link":
                    return self._json(200, core.share_link(a["c"], a["id"]))
                if p == "file/access":
                    return self._json(200, core.access(a["c"], a["id"]))
                if p == "file/grant":
                    return self._json(200, core.grant(a["c"], a["id"], a["to"]))
                if p == "file/approve":
                    return self._json(200, core.approve(a["c"], a["request"]))
                if p == "file/revoke":
                    return self._json(200, core.revoke(a["c"], a["id"], a["to"]))
                if p == "file/request":
                    return self._json(200, core.request_access(a["c"], a["id"], a.get("note", "")))
                if p == "join/preview":
                    return self._json(200, core.join_preview(a["link"]))
                if p == "join":
                    return self._json(200, core.join(a["link"], a.get("label")))
                if p == "retrust":
                    return self._json(200, core.retrust(a["c"], a["root"], a.get("confirm")))
                if p == "leave":
                    return self._json(200, core.leave(a["c"]))
                if p == "coll/update":
                    return self._json(200, core.update_joined(a["c"], a.get("label"), a.get("muted"),
                                                              a.get("pinned")))
                if p == "share":
                    return self._json(200, core.share(a["c"]))
                if p == "sync":
                    return self._json(200, core.sync(a.get("c")))
                if p == "draft":
                    return self._json(200, core.draft(a["c"], a["t"], a.get("body", {}),
                                                      a.get("refs", [])))
                if p == "confirm":
                    return self._json(200, core.confirm(a["token"]))
                if p == "cancel":
                    return self._json(200, core.cancel(a["token"]))
                if p == "delete":
                    return self._json(200, core.delete(a["c"], a["id"]))
                if p == "action":
                    return self._json(200, core.action(a["name"], a.get("args", {})))
                if p == "moderation":
                    if a.get("mode") not in ("honour", "ignore"):
                        raise ValueError("mode")
                    core.prefs["moderation"] = a["mode"]
                    core.save()
                    return self._json(200, {"ok": True})
                if p == "pay":
                    return self._json(200, core.pay_draft(a["c"], a["listing"]))
                if p == "pay/order":
                    return self._json(200, core.pay_order(a["token"], a.get("tx"), a.get("note")))
                if p == "pay/verify":
                    return self._json(200, core.verify_order(a["c"], a["order"]))
                if p == "registry/offer":
                    files = {k: base64.b64decode(v) for k, v in a["files"].items()}
                    return self._json(200, core.templates.offer(a["entry"], files))
                if p == "registry/approve":
                    return self._json(200, core.templates.approve(a["template"], a["ver"]))
                if p == "registry/withdraw":
                    return self._json(200, core.templates.apply_withdraw(a["withdraw"]))
            except (KeyError, ValueError, TypeError, F.FormatError, RegistryError) as e:
                return self._json(400, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            self._json(404, {"error": "not found"})
    return H


def serve(core, port=0, test_mode=False, sync_every=2.0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(core, test_mode))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if sync_every:
        def loop():
            while True:
                time.sleep(sync_every)
                try:
                    core.sync()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()
    return srv


def _preflight(data_dir) -> dict:
    """The setup check from the SRNet client (client-cli/srnet/preflight.py), read-only."""
    import sys
    d = os.environ.get("SRNET_CLIENT_DIR") or str(HERE.parents[1] / "client-cli")
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        from srnet import preflight
    except ImportError as e:
        return {"ok": False, "err": f"setup check not available: {e}"}
    rep = preflight.run()
    out = {"ok": True, "report": rep, "text": preflight.text(rep)}
    try:
        out["file"] = str(preflight.write_report(rep, home=Path(os.environ.get("SRNET_HOME") or Path.home() / ".srnet")))
    except (OSError, RuntimeError):
        pass
    return out


def _line(k, v):
    if k == "wraps":
        return f"shared with: {len(v)} key(s) (you included)"
    if k == "wrap":
        return f"grant to: {v['to'][:16]}…"
    return f"{k}: {v}"


def main():
    ap = argparse.ArgumentParser(description="typed-SNApp client core")
    ap.add_argument("--config", help="user client.json, layered on the shipped default-client.json")
    ap.add_argument("--defaults", help="use this defaults file instead of the shipped one")
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, default=48920)
    a = ap.parse_args()
    from .clientconfig import load as load_config
    core = Core(a.data, load_config(a.config, a.defaults))
    core.sync()
    srv = serve(core, a.port)
    print(f"typed client: http://127.0.0.1:{srv.server_address[1]}/  (author {core.me[:12]}…)",
          flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
