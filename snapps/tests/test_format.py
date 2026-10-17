import json, time, unittest
from pathlib import Path
from helpers import F

VEC = json.loads((Path(__file__).parent / "vectors/posts-v1.json").read_text())


class Format(unittest.TestCase):
    def test_vectors(self):
        alice = F.key_from_seed(bytes(range(32)))
        for v in VEC["valid"]:
            e = v["envelope"]
            self.assertEqual(F.verify_post(e, board="demo", pow_bits=8), e["id"])
            self.assertEqual(F.canonical(F.POST_DOMAIN, e["post"]).hex(), v["canonical_hex"])
        # Ed25519 is deterministic: re-signing reproduces the vector signature
        e0 = VEC["valid"][0]["envelope"]
        self.assertEqual(F.sign_post(alice, e0["post"])["sig"], e0["sig"])
        for v in VEC["invalid"]:
            with self.assertRaises(F.FormatError):
                F.verify_post(v["envelope"])

    def test_tamper_every_field(self):
        k = F.new_key()
        e = F.make_post(k, "demo", "original", ts=1792000000)
        for field, val in [("body", "changed"), ("ts", 1792000001), ("board", "other"),
                           ("reply_to", "y" * 26), ("author", F.pub_hex(F.new_key()))]:
            bad = json.loads(json.dumps(e)); bad["post"][field] = val
            with self.assertRaises(F.FormatError, msg=field):
                F.verify_post(bad)
        bad = json.loads(json.dumps(e)); bad["sig"] = ("0" if e["sig"][0] != "0" else "1") + e["sig"][1:]
        with self.assertRaises(F.FormatError):
            F.verify_post(bad)
        bad = dict(e, id="y" * 26)
        with self.assertRaises(F.FormatError):
            F.verify_post(bad)

    def test_body_rules(self):
        k = F.new_key()
        F.make_post(k, "demo", "x" * 500)
        for body in ["x" * 501, "", "   ", "bell\x07"]:
            with self.assertRaises(F.FormatError):
                F.make_post(k, "demo", body)
        e = F.make_post(k, "demo", "e\u0301")  # normalised to NFC before signing
        self.assertEqual(e["post"]["body"], "\u00e9")

    def test_extra_field_rejected(self):
        k = F.new_key()
        e = F.make_post(k, "demo", "hi")
        e["post"]["html"] = "<b>x</b>"
        e = F.sign_post(k, e["post"])
        with self.assertRaises(F.FormatError):
            F.verify_post(e)

    def test_pow(self):
        k = F.new_key()
        e = F.make_post(k, "demo", "work", pow_bits=12)
        F.verify_post(e, pow_bits=12)
        with self.assertRaises(F.FormatError):
            F.verify_post(e, pow_bits=20)
        weak = F.make_post(k, "demo", "lazy", pow_bits=0)
        with self.assertRaises(F.FormatError):
            F.verify_post(weak, pow_bits=8)
        # claiming more bits than were actually solved fails
        p = dict(e["post"], pow={"bits": 40, "salt": e["post"]["pow"]["salt"]})
        with self.assertRaises(F.FormatError):
            F.verify_post(F.sign_post(k, p), pow_bits=8)

    def test_checkpoint_rules(self):
        hk = F.new_key(); hp = F.pub_hex(hk)
        c = F.make_checkpoint(hk, "demo", 5, "a" * 64)
        F.verify_checkpoint(c, hp, "demo")
        with self.assertRaises(F.FormatError):
            F.verify_checkpoint(c, F.pub_hex(F.new_key()), "demo")
        old = F.make_checkpoint(hk, "demo", 5, "a" * 64, valid_s=10, now=int(time.time()) - 100)
        with self.assertRaises(F.FormatError):
            F.verify_checkpoint(old, hp, "demo")
        pin = F.CheckpointPin(5, "a" * 64)
        with self.assertRaises(F.FormatError):
            pin.check({"seq": 4, "head": "a" * 64})
        with self.assertRaises(F.FormatError):
            pin.check({"seq": 5, "head": "b" * 64})

    def test_delegation(self):
        root, hk = F.new_key(), F.new_key()
        d = F.make_delegation(root, "demo", F.pub_hex(hk), int(time.time()) + 60)
        self.assertEqual(F.verify_delegation(d, F.pub_hex(root), "demo"), F.pub_hex(hk))
        with self.assertRaises(F.FormatError):
            F.verify_delegation(d, F.pub_hex(F.new_key()), "demo")
        d["delegation"]["host"] = F.pub_hex(F.new_key())
        with self.assertRaises(F.FormatError):
            F.verify_delegation(d, F.pub_hex(root), "demo")


if __name__ == "__main__":
    unittest.main()
