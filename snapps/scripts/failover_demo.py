"""Two clients against a primary + mirror, killing the primary mid-demo.

  python3 scripts/failover_demo.py                        # all local (loopback)
  python3 scripts/failover_demo.py --host URL --mirror URL --root-pin HEX \
      --kill-cmd "ssh ... systemctl stop srnet-msgboard" --start-cmd "ssh ... systemctl start srnet-msgboard" \
      --admin-cmd "ssh ... sudo -u srmsgboard python3 -m mbhost.admin --data ... --board demo"
"""
import argparse, os, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mbfmt as F
from mbclient.client import Client
from mbclient.sync import http_json

ap = argparse.ArgumentParser()
for a in ("--host", "--mirror", "--root-pin", "--kill-cmd", "--start-cmd", "--admin-cmd"):
    ap.add_argument(a)
ap.add_argument("--board", default="demo")
a = ap.parse_args()
tmp = tempfile.mkdtemp(prefix="mbfail-")
local = None
if not a.host:
    from mbhost import host as H
    from mbhost.mirror import Mirror
    from mbhost.node import serve, url_of
    root, hk, d = H.load_or_make_keys(tmp + "/host", a.board)
    h = H.Host(tmp + "/host", a.board, hk, d, {"pow_bits": 12, "new_key_pow_bits": 14})
    hs = serve(h); a.host, a.root_pin = url_of(hs), F.pub_hex(root)
    m = Mirror(tmp + "/mirror", a.board, a.root_pin, [a.host]); a.mirror = url_of(serve(m))
    h.mirrors = [a.mirror]; h.run_ticker(1.0)
    port = hs.server_address[1]
    local = {"srv": hs, "h": h}

    def kill():
        local["srv"].shutdown(); local["srv"].server_close()

    def start():
        local["srv"] = serve(local["h"], "127.0.0.1", port)

    def admin(args):
        h.moderate(*args)
else:
    def kill(): subprocess.run(a.kill_cmd, shell=True, check=True)
    def start(): subprocess.run(a.start_cmd, shell=True, check=True)

    def admin(args):
        subprocess.run(f"{a.admin_cmd} {args[0]} {args[1]} {args[2]} --reason '{args[3]}'",
                       shell=True, check=True, stdout=subprocess.DEVNULL)

ok = True
def step(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}", flush=True)

def wait_mirror(seq, t=60):
    end = time.time() + t
    while time.time() < end:
        try:
            if http_json(a.mirror + "/v1/info")[1].get("seq", 0) >= seq:
                return True
        except OSError:
            pass
        time.sleep(1)
    return False

hosts = [a.host, a.mirror]
alice = Client(tmp + "/alice", a.board, a.root_pin, hosts)
bob = Client(tmp + "/bob", a.board, a.root_pin, hosts)
alice.sync(); bob.sync()
t = time.time(); p1 = alice.post("alice: failover demo, first post")
step("alice posts to primary", p1.get("ok"), f"seq={p1.get('seq')} ({time.time()-t:.1f}s incl. new-key PoW)")
step("primary pushes to mirror", wait_mirror(p1["seq"]))
r = bob.sync(); step("bob syncs", r["host"] == a.host and not r["error"], str(r["host"]))
kill(); print("   -- primary stopped --")
r = bob.sync(); step("bob fails over to mirror", r["host"] == a.mirror and not r["error"], str(r["host"]))
step("bob reads alice's post from mirror", (bob.thread(p1["id"])["root"] or {}).get("body") == "alice: failover demo, first post")
r = bob.post("bob: replying while the primary is down", p1["id"])
step("bob's reply kept in outbox (mirror can't forward)", r.get("queued"), r.get("error", ""))
start(); print("   -- primary restarted --"); time.sleep(2)
bob.sync(); step("outbox flushed after restart", bob.state()["outbox"] == 0)
alice.sync()
reps = alice.thread(p1["id"])["replies"]
step("alice sees bob's reply", [x["body"] for x in reps] == ["bob: replying while the primary is down"])
p2 = alice.post("alice: this one will be deleted")
step("alice deletes her post", alice.delete(p2["id"]).get("ok"))
bob.sync(); step("bob's copy redacted", all(x["id"] != p2["id"] for x in bob.timeline()))
admin(("hide", "post", reps[0]["id"], "demo-hide")); time.sleep(1)
alice.sync(); step("host hide honoured by alice", all(x["id"] != reps[0]["id"] for x in alice.timeline()))
alice.reveal(reps[0]["id"]); step("local override reveals it", any(x["id"] == reps[0]["id"] for x in alice.timeline()))
alice.reveal(reps[0]["id"], False)
seq = alice.pin.seq
step("mirror caught up after restart", wait_mirror(seq), f"seq={seq}")
m2 = Client(tmp + "/carol", a.board, a.root_pin, [a.mirror]); r = m2.sync()
step("fresh client verifies everything via mirror only", not r["error"] and m2.pin.seq == seq,
     f"seq={m2.pin.seq} records={r.get('records')}")
print("FAILOVER DEMO", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
