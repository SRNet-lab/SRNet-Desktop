#!/usr/bin/env python3
"""List your own collection in a directory (host opt-in).

The entry must be signed by the root key of the collection being listed, so only its
operator can list it. Prints the signed record; with --submit, posts it to the directory hub.

  python scripts/directory_entry.py --root-key root.key --directory URL --link srnet://... \
      --title "My board" [--desc ...] [--submit]
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mbfmt as F
from typed import joins
from typed.record import KIND_TYPES, make_record
from typed.sync import http_json, submit

ap = argparse.ArgumentParser()
ap.add_argument("--root-key", required=True, help="file with the 32-byte root seed of the listed collection")
ap.add_argument("--directory", required=True, help="directory collection base URL (…/c/<name>)")
ap.add_argument("--link", required=True); ap.add_argument("--title", required=True); ap.add_argument("--desc")
ap.add_argument("--submit", action="store_true")
a = ap.parse_args()
l = joins.parse(a.link)
key = F.key_from_seed(open(a.root_key, "rb").read()[:32])
if not l["root"] or F.pub_hex(key) != l["root"]:
    sys.exit("the root key doesn't match the #root= in the link")
_, d = http_json(a.directory + "/v1/delegation")
name = d["delegation"]["board"]
_, pol = http_json(a.directory + "/v1/policy")
env = make_record(key, KIND_TYPES["directory"], name, "directory.entry",
                  {"link": a.link, "title": a.title, "desc": a.desc}, [], pow_bits=int((pol or {}).get("pow_bits", 0)))
print(json.dumps(submit([a.directory], env) if a.submit else env))
