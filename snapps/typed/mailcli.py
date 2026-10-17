"""srnet mail: command-line mail (same identity and store as the SNApps window).

  srnet mail hosts URL [URL ...]     set your 1-3 mail hosts and register on them
  srnet mail address [--name N]      print your srnet://mail link
  srnet mail send TO -s SUBJ [-m TEXT | stdin] [--attach LINK]...
  srnet mail fetch                   pick up new mail from all your hosts
  srnet mail list [requests]         list inbox (or requests)
  srnet mail read ID                 show one message (signature already verified)
  srnet mail accept ID               accept the sender of a request
  srnet mail contact LINK [--name N] add a contact
  srnet mail name ADDR [NAME]        show / set a local petname for any srnet:// address
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .mail import MailError
from .mailclient import MailClient


def _client(data) -> MailClient:
    d = Path(data)
    d.mkdir(parents=True, exist_ok=True)
    kp = d / "author.key"  # same posting identity file as the typed core
    if not kp.exists():
        with open(os.open(kp, os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600), "wb") as f:
            f.write(os.urandom(32))
    try:  # shipped first contacts (and the user's client.json next to the data dir)
        from .clientconfig import load
        uc = d.parent / "client.json"
        mcfg = load(uc if uc.exists() else None)["mail"]
        contacts, hosts = mcfg.get("contacts", []), mcfg.get("hosts", [])
    except Exception:
        contacts, hosts = [], []
    # same default mail host as the app (default-client.json), used until you set your own
    return MailClient(d / "mail", kp.read_bytes(), hosts, contacts=contacts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="srnet mail", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True)
    sp = ap.add_subparsers(dest="c", required=True)
    p = sp.add_parser("hosts"); p.add_argument("urls", nargs="*")
    p = sp.add_parser("address"); p.add_argument("--name")
    p = sp.add_parser("send"); p.add_argument("to"); p.add_argument("-s", "--subject", default="")
    p.add_argument("-m", "--message"); p.add_argument("--attach", action="append", default=[])
    p.add_argument("--ttl-days", type=int)
    sp.add_parser("fetch")
    p = sp.add_parser("list"); p.add_argument("folder", nargs="?", default="inbox", choices=["inbox", "requests"])
    p = sp.add_parser("read"); p.add_argument("id")
    p = sp.add_parser("accept"); p.add_argument("id")
    p = sp.add_parser("contact"); p.add_argument("link"); p.add_argument("--name")
    p = sp.add_parser("name"); p.add_argument("addr"); p.add_argument("name", nargs="?")
    a = ap.parse_args(argv)
    mc = _client(a.data)
    try:
        if a.c == "hosts":
            if a.urls:
                mc.set_hosts(a.urls)
                for h, r in mc.register().items():
                    print(f"{h}: {r}")
            else:
                print("\n".join(mc.st["hosts"]) or "(no mail hosts set)")
        elif a.c == "address":
            link = mc.link(a.name)
            for h, r in mc.ensure_registered().items():  # open your inbox on the default host
                if r != "ok":
                    print(f"note: inbox on {h} not opened yet ({r}); `srnet mail fetch` retries",
                          file=sys.stderr)
            print(link)
        elif a.c == "send":
            body = a.message if a.message is not None else sys.stdin.read()
            r = mc.send(a.to, a.subject, body, a.attach, ttl=a.ttl_days and a.ttl_days * 86400)
            for h, v in r["hosts"].items():
                print(f"{h}: {v}")
            return 0 if r["ok"] else 1
        elif a.c == "fetch":
            r = mc.fetch()
            print(f"{r['new']} new, {r['dups']} duplicate copies, {len(r['rejected'])} rejected")
            for x in r["rejected"]:
                print(f"  rejected {x['msg_id'][:8]} from {x['host']}: {x['why']}")
            for h, e in r["errors"].items():
                print(f"  {h}: {e}")
        elif a.c == "list":
            for m in mc.folder(a.folder):
                t = time.strftime("%Y-%m-%d %H:%M", time.localtime(m["ts"]))
                print(f"{'*' if not m['read'] else ' '} {m['msg_id'][:12]}  {t}  {m['from_name']:<16} {m['subject']}")
        elif a.c in ("read", "accept"):
            ids = [i for i in mc.st["index"] if i.startswith(a.id)]
            if len(ids) != 1:
                print("no such message (or ambiguous id)", file=sys.stderr)
                return 1
            if a.c == "accept":
                mc.accept_sender(ids[0])
                print("accepted")
            else:
                m = mc.open(ids[0])
                print(f"From: {m['from_name']} ({m['from']}){'' if m['known'] else '  [not a contact]'}")
                print(f"Subject: {m['subject']}\nDate: {time.ctime(m['ts'])}\n\n{m['body']}")
                for x in m["attach"]:
                    print(f"\nAttachment: {x}")
        elif a.c == "contact":
            print(json.dumps(mc.add_contact(a.link, a.name)))
        elif a.c == "name":
            print(json.dumps(mc.petname(a.addr, a.name)))
    except MailError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
