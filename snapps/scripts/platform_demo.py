#!/usr/bin/env python3
"""End-to-end demo of every first typed SNApp through the client core.

Local (default): everything runs on loopback via typed.testbed.
Lab: --board-host/--board-pin/--hub-url/--hub-pin point board+hub at forwarded lab ports;
exit board, SNApp card and the HTML viewer site always run locally (adapters only).
Seeds for the admin/seller demo keys are read from --keys-dir (created if missing).
"""
import argparse, hashlib, json, os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mbfmt.core as F
from typed import testbed as T
from typed.core import Core


def seed(d, name):
    p = Path(d) / f"{name}.seed"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
            f.write(os.urandom(32))
    return p.read_bytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board-host", action="append", default=[])
    ap.add_argument("--board-pin")
    ap.add_argument("--board-name", default="board", help="board collection name on the host")
    ap.add_argument("--hub-url")
    ap.add_argument("--hub-pin")
    ap.add_argument("--keys-dir", default=os.path.expanduser("~/.srnet-demo-keys"))
    ap.add_argument("--print-pubs", action="store_true")
    a = ap.parse_args()
    adm, sel = seed(a.keys_dir, "admin"), seed(a.keys_dir, "seller")
    pubs = {"admin": F.pub_hex(F.key_from_seed(adm)), "seller": F.pub_hex(F.key_from_seed(sel))}
    if a.print_pubs:
        print(json.dumps(pubs)); return
    env = T.start(admins=[pubs["admin"]], sellers=[pubs["seller"]])
    cfg = T.client_config(env)
    if a.hub_url:
        for c in cfg["collections"]:
            if c["kind"] == "board" and a.board_host:
                c.update(hosts=a.board_host, root_pin=a.board_pin, name=a.board_name)
            elif c["kind"] in ("room", "wiki", "shop", "drop", "id"):
                c.update(hosts=[f"{a.hub_url}/c/{c['name']}"], root_pin=a.hub_pin)
    BN = a.board_name if a.board_host else "board"
    res, run = [], f"demo-{int(time.time())}"

    def core(name, s=None):
        d = env["tmp"] / "clients" / name
        d.mkdir(parents=True, exist_ok=True)
        if s:
            (d / "author.key").write_bytes(s); os.chmod(d / "author.key", 0o600)
        c = Core(str(d), cfg, autoconfirm=True); c.sync(); return c

    def check(kind, ok, detail=""):
        res.append((kind, bool(ok), detail)); print(f"[{'PASS' if ok else 'FAIL'}] {kind}: {detail}", flush=True)

    def post(c, coll, t, body, refs=()):
        r = c.draft(coll, t, body, list(refs))["result"]
        if not r.get("ok"):
            raise RuntimeError(f"{coll}/{t}: {r}")
        return r
    A, B, C = core("admin", adm), core("seller", sel), core("carol")
    steps = []
    def step(kind):
        def deco(fn):
            steps.append((kind, fn)); return fn
        return deco

    @step("board")
    def _():
        p = post(C, BN, "board.post", {"text": f"{run} board hello"})
        A.sync(BN); v = A.view(BN)["view"]["posts"]
        return any(x["id"] == p["id"] for x in v), f"post {p['id'][:8]} seen by 2nd client"

    @step("room")
    def _():
        post(C, "lobby", "room.join", {"nick": "carol"}); post(C, "lobby", "room.msg", {"text": f"{run} hi room"})
        denied = not C.draft("lobby", "room.topic", {"text": "x"}, [])["result"]["ok"]
        post(A, "lobby", "room.topic", {"text": f"{run} topic"})
        B.sync("lobby"); v = B.view("lobby")["view"]
        return denied and v["topic"]["text"] == f"{run} topic" and any(l["text"] == f"{run} hi room" for l in v["lines"]), "lines+admin topic, non-admin topic refused"

    @step("wiki")
    def _():
        slug = run.replace("-", "")
        post(C, "wiki", "wiki.page", {"slug": slug, "title": "Demo"})
        r1 = post(C, "wiki", "wiki.rev", {"slug": slug, "text": "v1"})
        post(C, "wiki", "wiki.rev", {"slug": slug, "text": "v2"}, [r1["id"]])
        post(A, "wiki", "wiki.rev", {"slug": slug, "text": "stale"}, [r1["id"]])
        post(A, "wiki", "wiki.protect", {"slug": slug, "on": True})
        B.sync("wiki"); p = B.view("wiki")["view"]["pages"][slug]
        return p["text"] == "v2" and p["protected"] and p["conflicts"], "revs, stale-parent conflict, protect"

    @step("profile")
    def _():
        rec = F.new_key()
        from typed import views
        post(C, "people", "id.profile", {"name": "carol", "bio": run, "next_key_hash": views.key_hash(F.pub_hex(rec))})
        B.sync("people")
        ppl = B.view("people")["view"]["people"]
        return any(x["label"].startswith("carol·") for x in ppl), "profile with recovery commitment (rotation/recovery covered by tests)"

    @step("storefront")
    def _():
        body = {"title": f"{run} stickers", "desc": "demo", "price": "0.001", "currency": "ETH",
                "chain_id": 11155111, "pay_to": "0x" + "ab" * 20}
        l = post(B, "store", "shop.listing", body)
        no_main = not B.draft("store", "shop.listing", dict(body, chain_id=1), [])["result"]["ok"]
        post(C, "store", "shop.review", {"rating": 4, "text": "nice"}, [l["id"]])
        C.sync("store"); s = C.pay_draft("store", l["id"])["sheet"]
        return no_main and s["network"] == "Sepolia", f"listing, review, testnet pay sheet (not sent), mainnet refused"

    @step("file drop")
    def _():
        data = (run + "\n").encode() * 20
        assert C.upload("files", data, "demo.txt", "text/plain", run, mode="gated",
                        recipients=[B.enc_pub])["result"]["ok"]
        B.sync("files"); it = sorted(B.view("files")["view"]["items"], key=lambda i: i["ts"])[-1]
        host_blob = B.blob("files", it["sha256"])
        return (B.open_file("files", it["id"])[0] == data and run.encode() not in host_blob,
                "encrypted upload, host holds ciphertext only, recipient decrypts")

    @step("exit board")
    def _():
        A.sync("exits"); v = A.view("exits")
        return bool(v.get("view")) and not v.get("error"), "vendored exit-board format read as typed view (local)"

    @step("snapp card")
    def _():
        if __import__("typed.adapters").adapters.cardfmt is None:
            return True, "skipped: card verifier not bundled"
        v = A.view("rooms-card")
        return bool(v.get("view")) and not v.get("error"), "vendored card verified (local)"

    @step("html viewer")
    def _():
        v = A.viewer("demo.sesh/")
        page = v if isinstance(v, str) else json.dumps(v)
        body = page.lower().split("</style>", 1)[-1]
        import re
        clean = "<script" not in body and "http" not in body and not re.search(r"\son\w+=", body)
        return clean and "not verified content" in page and "plain page" in body, f"hostile page sanitised ({len(page)} B), banner present (local)"

    for kind, fn in steps:
        try:
            ok, d = fn(); check(kind, ok, d)
        except Exception as e:
            check(kind, False, repr(e))
    T.stop(env)
    n = sum(ok for _, ok, _ in res)
    print(f"{n}/{len(res)} passed")
    sys.exit(0 if n == len(res) else 1)


if __name__ == "__main__":
    main()
