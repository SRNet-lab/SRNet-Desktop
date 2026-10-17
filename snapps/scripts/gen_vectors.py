"""Regenerate tests/vectors/posts-v1.json (deterministic: fixed seeds, ts and PoW salt prefix)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mbfmt as F

alice = F.key_from_seed(bytes(range(32)))
bob = F.key_from_seed(bytes([7]) * 32)
def mk(key, body, reply_to=None, ts=1792000000, bits=8):
    post = {"v": 1, "board": "demo", "author": F.pub_hex(key), "ts": ts, "body": body,
            "reply_to": reply_to, "pow": {"bits": 0, "salt": ""}}
    post = F.solve_pow(post, bits, prefix="vec")
    return F.sign_post(key, post)
p1 = mk(alice, "hello, SRNet")
p2 = mk(bob, "reply with unicode ✓ and <b>markup</b> that must stay text", p1["id"], 1792000060)
vec = {"note": "seeds: alice=bytes(0..31), bob=bytes([7])*32; signing domain "
               f"'{F.POST_DOMAIN}\\n' + canonical JSON",
       "valid": [{"envelope": e, "canonical_hex": F.canonical(F.POST_DOMAIN, e["post"]).hex()}
                 for e in (p1, p2)]}
bad = json.loads(json.dumps(p1)); bad["post"]["body"] = "hello, SRNet!"
vec["invalid"] = [{"why": "body edited after signing", "envelope": bad}]
Path(__file__).resolve().parent.parent.joinpath("tests/vectors/posts-v1.json").write_text(
    json.dumps(vec, indent=2, ensure_ascii=False) + "\n")
print(p1["id"], p2["id"])
