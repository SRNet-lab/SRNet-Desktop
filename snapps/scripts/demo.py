"""Loopback demo: one host, two clients (alice, bob) posting and replying, all verified.

  python3 scripts/demo.py                      # starts its own host on 127.0.0.1
  python3 scripts/demo.py --host URL --root-pin HEX [--board demo]   # use an existing host
  add --serve to keep both client UIs running for a browser.
"""
import argparse, json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mbfmt as F
from mbhost import host as H
from mbclient.client import Client, serve

ap = argparse.ArgumentParser()
ap.add_argument("--host"); ap.add_argument("--root-pin"); ap.add_argument("--board", default="demo")
ap.add_argument("--pow-bits", type=int, default=16); ap.add_argument("--serve", action="store_true")
a = ap.parse_args()
tmp = tempfile.mkdtemp(prefix="mbdemo-")
if a.host:
    url, pin = a.host, a.root_pin
else:
    root, hk, d = H.load_or_make_keys(tmp + "/host", a.board)
    srv = H.serve(H.Host(tmp + "/host", a.board, hk, d, {"pow_bits": a.pow_bits, "new_key_pow_bits": a.pow_bits}))
    url, pin = f"http://127.0.0.1:{srv.server_address[1]}", F.pub_hex(root)
print("host", url, "root pin", pin[:16] + "…")
dead = "http://127.0.0.1:9"  # first entry in the failover list is down on purpose
alice = Client(tmp + "/alice", a.board, pin, [dead, url])
bob = Client(tmp + "/bob", a.board, pin, [dead, url])
t = time.time(); r1 = alice.post("Hello from alice — first post on the SRNet board.")
print(f"alice post  ok={r1['ok']} id={r1.get('id')} seq={r1.get('seq')} ({time.time()-t:.1f}s incl. PoW)")
print("bob sync   ", (lambda r: {k: r.get(k) for k in ("host", "added", "error")})(bob.sync()))
r2 = bob.post("Hi alice! Replying from bob. <script>alert(1)</script> stays plain text.", r1["id"])
print(f"bob reply   ok={r2['ok']} id={r2.get('id')} seq={r2.get('seq')}")
alice.toggle("follows", bob.me, True)
print("alice sync ", (lambda r: {k: r.get(k) for k in ("host", "added", "error")})(alice.sync()))
th = alice.thread(r1["id"])
print("alice thread:", th["root"]["body"], "| replies:", [x["body"] for x in th["replies"]])
alice.toggle("mutes", bob.me, True)
print("alice mutes bob -> timeline authors:", {p["author"][:8] for p in alice.timeline()})
alice.toggle("mutes", bob.me, False)
ok = r1["ok"] and r2["ok"] and len(th["replies"]) == 1 and alice.pin.seq == bob.pin.seq
print("DEMO", "PASS" if ok else "FAIL", f"(both clients verified to seq {alice.pin.seq})")
if a.serve:
    for n, c in (("alice", alice), ("bob", bob)):
        print(n, "UI: http://127.0.0.1:%d/" % serve(c).server_address[1])
    threading.Event().wait()
sys.exit(0 if ok else 1)
