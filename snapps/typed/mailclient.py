"""SRNet mail client: your address, contacts, petnames, send to every host, fetch + dedupe.

On disk (data_dir): received mail is kept SEALED (msgs/<msg_id>.bin) and only opened in
memory when you read it.  index.json holds msg_id, folder, sender key and time; no content.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import filecrypt as FC, mail as M
from .sync import http_json
import mbfmt as F

FOLDERS = ("inbox", "requests")


class MailClient:
    def __init__(self, data_dir, seed: bytes, hosts=(), clock=time.time, http=http_json,
                 attacher=None, contacts=()):
        self.dir = Path(data_dir)
        (self.dir / "msgs").mkdir(parents=True, exist_ok=True)
        self.key = F.key_from_seed(seed)
        self.xkey = FC.x25519_from_seed(seed)
        self.me = F.pub_hex(self.key)
        self.clock, self.http = clock, http
        self.attacher = attacher  # (bytes, filename, mime) -> encrypted file-drop link
        self.st = {"hosts": [], "contacts": {}, "petnames": {}, "index": {}, "blocked": [],
                   "host_ids": {}, "seeded": [], "registered": []}
        p = self.dir / "mail.json"
        if p.exists():
            self.st.update(json.loads(p.read_text(encoding="utf-8")))
        if hosts and not self.st["hosts"]:
            self.set_hosts(list(hosts), save=False)
        self.seed_contacts(contacts)
        self.save()

    def seed_contacts(self, contacts):
        """Shipped first contacts (config mail.contacts): each is added once, so a contact the
        user later removes or renames stays that way. Tags reach hosts on the next register()."""
        for c in contacts or ():
            try:
                env, suggested = M.parse_link(c["link"])
                card = M.verify_card(env)
            except Exception:
                continue  # a bad shipped card is skipped, never fatal
            ed = card["ed"]
            if ed in self.st["seeded"] or ed == self.me:
                continue
            self.st["seeded"].append(ed)
            if ed in self.st["contacts"] or ed in self.st["blocked"]:
                continue
            self.st["contacts"][ed] = {"x": card["x"], "hosts": card["hosts"], "ts": card["ts"]}
            self.st["petnames"].setdefault(f"srnet://mail/{ed}", str(c.get("name") or suggested or "")[:64] or ed[:12])

    def save(self):
        p = self.dir / "mail.json"
        tmp = p.with_suffix(".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_BINARY', 0) | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(self.st, f)
        os.replace(tmp, p)

    # ---- address / hosts ----
    def set_hosts(self, hosts, save=True):
        hosts = [M.check_host_url(h) for h in hosts]
        if not 1 <= len(hosts) <= M.MAX_HOSTS:
            raise M.MailError(f"list 1 to {M.MAX_HOSTS} mail hosts")
        self.st["hosts"] = hosts
        if save:
            self.save()

    def card(self) -> dict:
        if not self.st["hosts"]:
            raise M.MailError("no mail hosts set (srnet mail hosts URL ...)")
        return M.make_card(self.key, self.xkey, self.st["hosts"], ts=self.clock())

    def link(self, name=None) -> str:
        return M.card_link(self.card(), name)

    def host_id(self, h) -> str:
        if h not in self.st["host_ids"]:
            code, info = self.http(h + "/v1/mail/info")
            if code != 200 or info.get("role") != "mail" or not M.HEX64.match(str(info.get("host"))):
                raise M.MailError(f"{h}: not a mail host")
            self.st["host_ids"][h] = info["host"]  # pinned on first use
            self.save()
        return self.st["host_ids"][h]

    def _auth(self, h, op, args=None):
        code, c = self.http(h + "/v1/mail/challenge", {"to": self.me})
        if code != 200:
            raise M.MailError(f"{h}: {c.get('error')}")
        if c.get("host") != self.host_id(h):
            raise M.MailError(f"{h}: host key changed; refusing")
        code, r = self.http(h + "/v1/mail/auth", M.sign_auth(self.key, c["host"], op, c["nonce"], args or {}))
        if code != 200:
            raise M.MailError(f"{h}: {r.get('error')}")
        return r

    def register(self) -> dict:
        out = {}
        for h in self.st["hosts"]:
            try:
                self._auth(h, "register")
                self._push_tags(h)
                out[h] = "ok"
                if h not in self.st.setdefault("registered", []):
                    self.st["registered"].append(h)
            except Exception as e:
                out[h] = f"error: {e}"
        self.save()
        return out

    def ensure_registered(self) -> dict:
        """Open an inbox on any host not yet registered (e.g. the shipped default host), so
        people can write to you before you ever run `mail hosts`. Best effort; retried later."""
        todo = [h for h in self.st["hosts"] if h not in self.st.setdefault("registered", [])]
        out = {}
        for h in todo:
            try:
                self._auth(h, "register")
                self._push_tags(h)
                self.st["registered"].append(h)
                out[h] = "ok"
            except Exception as e:  # noqa: BLE001
                out[h] = f"error: {e}"
        if out:
            self.save()
        return out

    def _push_tags(self, h):
        hid = self.host_id(h)
        tags = [M.contact_tag(self.xkey, c["x"], self.me, hid) for c in self.st["contacts"].values()]
        self._auth(h, "tags", {"tags": tags})

    # ---- contacts / petnames ----
    def add_contact(self, link_or_env, name=None) -> dict:
        env, suggested = (M.parse_link(link_or_env) if isinstance(link_or_env, str)
                          else (link_or_env, None))
        c = M.verify_card(env)
        if c["ed"] == self.me:
            raise M.MailError("that is your own address")
        old = self.st["contacts"].get(c["ed"])
        if old and old.get("ts", 0) > c["ts"]:
            c = old  # keep the newer card
        self.st["contacts"][c["ed"]] = {"x": c["x"], "hosts": c["hosts"], "ts": c["ts"]}
        key = f"srnet://mail/{c['ed']}"
        if name or (suggested and key not in self.st["petnames"]):
            self.st["petnames"][key] = (name or suggested)[:64]
        if c["ed"] in self.st["blocked"]:
            self.st["blocked"].remove(c["ed"])
        for mid, ent in self.st["index"].items():  # move their pending requests to the inbox
            if ent["from"] == c["ed"] and ent["folder"] == "requests":
                ent["folder"] = "inbox"
        self.save()
        self.register()  # refresh tags on every host
        return {"ok": True, "contact": c["ed"], "name": self.st["petnames"].get(key)}

    def remove_contact(self, ed, block=False):
        self.st["contacts"].pop(ed, None)
        if block and ed not in self.st["blocked"]:
            self.st["blocked"].append(ed)
        self.save()
        self.register()
        return {"ok": True}

    def petname(self, addr, name=None):
        """Set (name given) or clear (name == "") a local name for any srnet:// address."""
        k = M.address_key(addr)
        if name == "":
            self.st["petnames"].pop(k, None)
        elif name:
            self.st["petnames"][k] = name.strip()[:64]
        else:
            try:  # suggested name from the link, only if we have none
                from urllib.parse import parse_qs, urlparse
                s = parse_qs(urlparse(addr).query).get("name", [None])[0]
                if s and k not in self.st["petnames"]:
                    self.st["petnames"][k] = s[:64]
            except ValueError:
                pass
        self.save()
        return {"key": k, "name": self.st["petnames"].get(k)}

    def name_of(self, ed) -> str:
        return self.st["petnames"].get(f"srnet://mail/{ed}") or ed[:12] + "…"

    # ---- send ----
    def _resolve(self, to) -> dict:
        if to.startswith("srnet://mail/") and to.rsplit("/", 1)[1] in self.st["contacts"]:
            to = to.rsplit("/", 1)[1]  # a contact's bare address (no card query) is enough
        elif to.startswith("srnet://"):
            env, sugg = M.parse_link(to)
            return M.verify_card(env)
        for k, n in self.st["petnames"].items():  # a petname works as an address
            if n == to and k.startswith("srnet://mail/"):
                to = k.rsplit("/", 1)[1]
        c = self.st["contacts"].get(to)
        if not c:
            raise M.MailError("unknown recipient: paste their srnet://mail link")
        return {"ed": to, **c}

    def send(self, to, subject, body, attach=(), reply_to=None, ttl=None, stamp=None) -> dict:
        card = self._resolve(to)
        inner = M.make_message(self.key, self.xkey, self.st["hosts"], card, subject, body,
                               attach, reply_to, ts=self.clock())
        blob = M.seal(inner, card["x"])
        res = {}
        for h in card["hosts"]:
            try:
                tag = M.contact_tag(self.xkey, card["x"], card["ed"], self.host_id(h))
                req = {"to": card["ed"], "msg_id": inner["msg_id"], "blob": M.b64e(blob), "tag": tag}
                if ttl:
                    req["ttl"] = int(ttl)
                if stamp is not None:
                    req["stamp"] = stamp
                code, r = self.http(h + "/v1/mail/put", req)
                res[h] = r.get("pile", "dup") if code == 200 else f"error {code}: {r.get('error')}"
            except Exception as e:
                res[h] = f"error: {e}"
        ok = any(not v.startswith("error") for v in res.values())
        return {"ok": ok, "msg_id": inner["msg_id"], "hosts": res}

    # ---- receive ----
    def fetch(self) -> dict:
        self.ensure_registered()
        new, dups, rejected, errors = 0, 0, [], {}
        for h in self.st["hosts"]:
            try:
                lst = self._auth(h, "list")["msgs"]
                ids = [m["msg_id"] for m in lst]
                for i in range(0, len(ids), 64):
                    got = self._auth(h, "fetch", {"ids": ids[i:i + 64]})["msgs"]
                    done = []
                    for m in got:
                        r = self._accept(m["msg_id"], M.b64d(m["blob"]))
                        if r == "new":
                            new += 1
                        elif r == "dup":
                            dups += 1
                        else:
                            rejected.append({"msg_id": m["msg_id"], "host": h, "why": r})
                        done.append(m["msg_id"])
                    self.save()  # index + sealed files are on disk before the host may delete
                    if done:
                        self._auth(h, "ack", {"ids": done})
            except Exception as e:
                errors[h] = str(e)
        self.save()
        return {"new": new, "dups": dups, "rejected": rejected, "errors": errors}

    def _accept(self, mid, blob) -> str:
        if not M.MSGID.match(mid):
            return "bad msg_id"
        if mid in self.st["index"]:
            return "dup"
        try:
            inner = M.open_sealed(blob, self.xkey, mid, self.me, now=self.clock())
        except M.MailError as e:
            return str(e)
        if inner["from"] in self.st["blocked"]:
            return "blocked sender"
        folder = "inbox" if inner["from"] in self.st["contacts"] else "requests"
        p = self.dir / "msgs" / (mid + ".bin")
        with open(os.open(p, os.O_WRONLY | os.O_CREAT | getattr(os, 'O_BINARY', 0) | os.O_TRUNC, 0o600), "wb") as f:
            f.write(blob)  # kept sealed
        self.st["index"][mid] = {"folder": folder, "from": inner["from"], "ts": inner["ts"],
                                 "got": int(self.clock()), "read": False}
        return "new"

    def open(self, mid) -> dict:
        ent = self.st["index"][mid]
        blob = (self.dir / "msgs" / (mid + ".bin")).read_bytes()
        inner = M.open_sealed(blob, self.xkey, mid, self.me, now=ent["got"])
        if not ent["read"]:
            ent["read"] = True
            self.save()
        return {"msg_id": mid, "folder": ent["folder"], "from": inner["from"],
                "from_name": self.name_of(inner["from"]), "known": inner["from"] in self.st["contacts"],
                "ts": inner["ts"], "subject": inner["subject"], "body": inner["body"],
                "attach": inner["attach"], "reply_to": inner["reply_to"],
                "reply_card": {"ed": inner["from"], "x": inner["from_x"], "hosts": inner["from_hosts"]}}

    def folder(self, name) -> list:
        if name not in FOLDERS:
            raise ValueError("folder")
        out = []
        for mid, e in sorted(self.st["index"].items(), key=lambda kv: -kv[1]["ts"]):
            if e["folder"] == name:
                try:
                    m = self.open_preview(mid)
                except Exception:
                    continue
                out.append(m)
        return out

    def open_preview(self, mid):
        ent = self.st["index"][mid]
        blob = (self.dir / "msgs" / (mid + ".bin")).read_bytes()
        inner = M.open_sealed(blob, self.xkey, mid, self.me, now=ent["got"])
        return {"msg_id": mid, "from": inner["from"], "from_name": self.name_of(inner["from"]),
                "ts": inner["ts"], "subject": inner["subject"], "read": ent["read"]}

    def accept_sender(self, mid):
        """Accept the sender of a request using the reply card inside their signed message."""
        m = self.open(mid)
        rc = m["reply_card"]
        if not rc["hosts"]:
            raise M.MailError("sender gave no mail hosts")
        self.st["contacts"][rc["ed"]] = {"x": rc["x"], "hosts": rc["hosts"], "ts": m["ts"]}
        for e in self.st["index"].values():
            if e["from"] == rc["ed"]:
                e["folder"] = "inbox"
        self.save()
        self.register()
        return {"ok": True}

    def reply(self, mid, body, subject=None, attach=()):
        m = self.open(mid)
        rc = m["reply_card"]
        if rc["ed"] not in self.st["contacts"]:
            self.st["contacts"][rc["ed"]] = {"x": rc["x"], "hosts": rc["hosts"], "ts": m["ts"]}
            self.save()
        subj = subject or (m["subject"] if m["subject"].startswith("Re: ") else "Re: " + m["subject"])
        return self.send(rc["ed"], subj[:M.MAX_SUBJECT], body, attach, reply_to=mid)

    def delete(self, mid):
        self.st["index"].pop(mid, None)
        try:
            (self.dir / "msgs" / (mid + ".bin")).unlink()
        except FileNotFoundError:
            pass
        self.save()
        return {"ok": True}

    def summary(self) -> dict:
        return {"me": self.me, "hosts": self.st["hosts"],
                "link": self.link() if self.st["hosts"] else None,
                "contacts": [{"ed": e, "name": self.name_of(e), "hosts": c["hosts"]}
                             for e, c in self.st["contacts"].items()],
                "petnames": self.st["petnames"],
                "unread": {f: sum(1 for e in self.st["index"].values() if e["folder"] == f and not e["read"])
                           for f in FOLDERS}}

    # ---- local API for the client shell (/api/mail/<op>) ----
    def api(self, op, a):
        if op == "summary":
            return self.summary()
        if op == "hosts":
            self.set_hosts(a["hosts"])
            return self.register()
        if op == "register":
            return self.register()
        if op == "fetch":
            return self.fetch()
        if op == "folder":
            return {"msgs": self.folder(a.get("folder", "inbox"))}
        if op == "open":
            return self.open(a["id"])
        if op == "attach":
            if not self.attacher:
                raise M.MailError("join a file drop first: attachments are stored there, encrypted")
            return {"ok": True, "link": self.attacher(M.b64d(a["data"]), a.get("name", "file")[:128],
                                                      a.get("mime", "application/octet-stream")[:80])}
        if op == "send":
            return self.send(a["to"], a.get("subject", ""), a.get("body", ""), a.get("attach", []))
        if op == "reply":
            return self.reply(a["id"], a.get("body", ""), attach=a.get("attach", []))
        if op == "accept":
            return self.accept_sender(a["id"])
        if op == "contact":
            return self.add_contact(a["link"], a.get("name"))
        if op == "uncontact":
            return self.remove_contact(a["ed"], bool(a.get("block")))
        if op == "delete":
            return self.delete(a["id"])
        if op == "petname":
            return self.petname(a["addr"], a.get("name"))
        raise ValueError(f"unknown mail op {op}")
