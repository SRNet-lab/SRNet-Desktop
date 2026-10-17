"""Encrypted file drops: host can't read, public link, grant/revoke, request flow, tamper, wrong key."""
import hashlib
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mbfmt as F  # noqa: E402
from typed import filecrypt as FC, testbed as T  # noqa: E402
from typed.core import Core  # noqa: E402

SECRET = b"the launch codes are 0000 " * 200


class Format(unittest.TestCase):
    def test_roundtrip_and_buckets(self):
        for n in (0, 1, 5000, 70000, 300000):
            data = os.urandom(n)
            blob, key = FC.encrypt(data, "a.bin", "application/octet-stream")
            self.assertEqual(FC.decrypt(blob, key)[0], data)
            body = len(blob) - 8
            self.assertIn(FC.bucket(n + 60), [body - k * 16 for k in range(1, 200)])
        a, _ = FC.encrypt(b"x" * 100, "a", "text/plain")
        b, _ = FC.encrypt(b"x" * 3000, "much-longer-name.txt", "text/plain")
        self.assertEqual(len(a), len(b))  # same bucket: size and name hidden

    def test_wrong_key_and_tamper(self):
        blob, key = FC.encrypt(SECRET, "s.txt", "text/plain")
        with self.assertRaises(FC.CryptoError):
            FC.decrypt(blob, os.urandom(32))
        bad = bytearray(blob); bad[100] ^= 1
        with self.assertRaises(FC.CryptoError):
            FC.decrypt(bytes(bad), key)
        with self.assertRaises(FC.CryptoError):
            FC.decrypt(blob[:-1], key)
        big, k2 = FC.encrypt(os.urandom(200000), "b", "x/y")
        chunk = 64 * 1024 + 16  # swapping two chunks is caught
        sw = big[:8] + big[8 + chunk:8 + 2 * chunk] + big[8:8 + chunk] + big[8 + 2 * chunk:]
        with self.assertRaises(FC.CryptoError):
            FC.decrypt(sw, k2)

    def test_wrap_bound_to_file_and_recipient(self):
        a, b = FC.x25519_from_seed(b"a" * 32), FC.x25519_from_seed(b"b" * 32)
        key = os.urandom(32)
        w = FC.wrap(key, FC.pub_hex(a), "11" * 32)
        self.assertEqual(FC.unwrap(w, a, "11" * 32), key)
        with self.assertRaises(FC.CryptoError):
            FC.unwrap(w, b, "11" * 32)
        with self.assertRaises(FC.CryptoError):
            FC.unwrap(w, a, "22" * 32)

    def test_link(self):
        _, key = FC.encrypt(b"hi", "h", "text/plain")
        url = FC.link("http://x.sesh", "ab" * 32, key)
        base, sha, k = FC.parse_link(url)
        self.assertNotIn("#", base)
        self.assertEqual((sha, k), ("ab" * 32, key))


class Drops(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = T.start(mirror=False)
        cls.cfg = T.client_config(cls.env)

    @classmethod
    def tearDownClass(cls):
        T.stop(cls.env) if hasattr(T, "stop") else None

    def core(self, name):
        d = self.env["tmp"] / "fc" / name
        d.mkdir(parents=True, exist_ok=True)
        c = Core(str(d), self.cfg, autoconfirm=True)
        c.sync()
        return c

    def files(self, c):
        c.sync("files")
        return c.view("files")["view"]["items"]

    def blobdir(self):
        return Path(self.env["tmp"]) / "hub" / "files" / "blobs"

    def latest(self, c):
        return sorted(self.files(c), key=lambda i: i["ts"])[-1]

    def test_host_never_sees_plaintext_or_name(self):
        a = self.core("owner1")
        a.upload("files", SECRET, "secret-plan.txt", "text/plain", mode="gated")
        it = self.latest(a)
        stored = (self.blobdir() / it["sha256"]).read_bytes()
        self.assertNotIn(b"launch codes", stored)
        self.assertNotIn(b"secret-plan", stored)
        recs = json.dumps([r for r in a.colls["files"].store.records_since(0, 1 << 30)])
        self.assertNotIn("secret-plan", recs)
        self.assertTrue(it["encrypted"]) ; self.assertIsNone(it["name"])
        self.assertEqual(a.open_file("files", it["id"])[:2], (SECRET, "secret-plan.txt"))

    def test_template_view_has_no_keys(self):
        a = self.core("owner2")
        a.upload("files", SECRET, "x.txt", "text/plain", mode="gated")
        v = json.dumps(a.view("files"))
        self.assertNotIn('"wraps"', v); self.assertNotIn('"ct"', v); self.assertNotIn('"epk"', v)

    def test_link_mode(self):
        a, b = self.core("owner3"), self.core("stranger3")
        a.upload("files", SECRET, "pub.txt", "text/plain", mode="link")
        it = self.latest(a)
        link = a.share_link("files", it["id"])["link"]
        self.assertIn("#k=", link)
        self.assertEqual(b.open_link(link)[0], SECRET)
        with self.assertRaises(F.FormatError):  # without the fragment, nobody can read it
            b.open_file("files", it["id"])
        wrong = link.split("#k=")[0] + "#k=" + FC.link("x", "00" * 32, os.urandom(32)).split("#k=")[1]
        with self.assertRaises(F.FormatError):
            b.open_link(wrong)

    def test_grant_request_revoke(self):
        a, b, c = self.core("owner4"), self.core("friend4"), self.core("other4")
        a.upload("files", SECRET, "g.txt", "text/plain", mode="gated", recipients=[b.enc_pub])
        it = self.latest(a)
        b.sync("files")
        self.assertEqual(b.open_file("files", it["id"])[0], SECRET)
        with self.assertRaises(F.FormatError):
            c.open_file("files", it["id"])
        # request -> approve -> c can open
        c.sync("files")
        c.request_access("files", it["id"], "please")
        a.sync("files")
        req = a.access("files", it["id"])["requests"]
        self.assertEqual(len(req), 1)
        self.assertEqual(len([x for x in self.files(a) if x["id"] == it["id"]][0]["requests"]), 1)
        a.approve("files", req[0]["id"])
        c.sync("files")
        self.assertEqual(c.open_file("files", it["id"])[0], SECRET)
        # revoke c, publish a new version: c can't open v2, b can; c keeps what they already had
        a.revoke("files", it["id"], c.enc_pub)
        a.sync("files")
        a.new_version("files", it["id"], SECRET + b"v2", "g.txt", "text/plain")
        v2 = self.latest(a)
        self.assertEqual(v2["prev"], it["id"])
        b.sync("files"); c.sync("files")
        self.assertEqual(b.open_file("files", v2["id"])[0], SECRET + b"v2")
        with self.assertRaises(F.FormatError):
            c.open_file("files", v2["id"])
        self.assertEqual(c.open_file("files", it["id"])[0], SECRET)  # honest: v1 stays readable

    def test_only_owner_grants(self):
        a, b = self.core("owner5"), self.core("x5")
        a.upload("files", SECRET, "o.txt", "text/plain", mode="gated", recipients=[b.enc_pub])
        it = self.latest(a)
        b.sync("files")
        with self.assertRaises(F.FormatError):
            b.grant("files", it["id"], "ab" * 32)

    def test_tampered_blob_rejected(self):
        a = self.core("owner6")
        a.upload("files", SECRET, "t.txt", "text/plain", mode="gated")
        it = self.latest(a)
        p = self.blobdir() / it["sha256"]
        orig = p.read_bytes()
        bad = bytearray(orig); bad[50] ^= 1
        p.write_bytes(bytes(bad))
        shutil.rmtree(a.blob_cache); a.blob_cache.mkdir()
        try:
            with self.assertRaises(F.FormatError):
                a.open_file("files", it["id"])  # hash check fails -> unavailable
        finally:
            p.write_bytes(orig)

    def test_plain_opt_in(self):
        a = self.core("owner7")
        a.upload("files", b"hello world", "hello.txt", "text/plain", mode="plain")
        it = self.latest(a)
        self.assertFalse(it["encrypted"]); self.assertEqual(it["name"], "hello.txt")
        self.assertEqual((self.blobdir() / it["sha256"]).read_bytes(), b"hello world")


if __name__ == "__main__":
    unittest.main()
