"""SRNet mail: format, sealing, forgery, inbox auth, piles, quotas, TTL, multi-host e2e."""
import base64
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mbfmt as F  # noqa: E402
from typed import filecrypt as FC, mail as M, mailhost as MH  # noqa: E402
from typed.mailclient import MailClient  # noqa: E402
from typed.sync import http_json  # noqa: E402
from helpers import tmpdir  # noqa: E402

SECRET = "the launch codes are 0000"


def ident(seed=None):
    seed = seed or os.urandom(32)
    return F.key_from_seed(seed), FC.x25519_from_seed(seed)


class Format(unittest.TestCase):
    def setUp(self):
        self.ak, self.ax = ident()
        self.bk, self.bx = ident()
        self.bcard = M.make_card(self.bk, self.bx, ["http://127.0.0.1:1"])

    def test_link_roundtrip_and_tamper(self):
        link = M.card_link(self.bcard, "Bob")
        env, name = M.parse_link(link)
        self.assertEqual(env["card"]["ed"], F.pub_hex(self.bk))
        self.assertEqual(name, "Bob")
        bad = link.replace("127.0.0.1%3A1", "127.0.0.1%3A2")
        with self.assertRaises(M.MailError):
            M.parse_link(bad)
        with self.assertRaises(M.MailError):
            M.make_card(self.bk, self.bx, ["http://a", "http://b", "http://c", "http://d"])

    def test_seal_open_and_padding(self):
        c = self.bcard["card"]
        a = M.make_message(self.ak, self.ax, ["http://x"], c, "hi", "short")
        b = M.make_message(self.ak, self.ax, ["http://x"], c, "hi", "x" * 500)
        sa, sb = M.seal(a, c["x"]), M.seal(b, c["x"])
        self.assertEqual(len(sa), len(sb))  # same bucket
        self.assertNotIn(b"short", sa)
        got = M.open_sealed(sa, self.bx, a["msg_id"], c["ed"])
        self.assertEqual(got["body"], "short")
        with self.assertRaises(M.MailError):  # wrong recipient key
            M.open_sealed(sa, self.ax, a["msg_id"], c["ed"])
        with self.assertRaises(M.MailError):  # host relabels msg_id
            M.open_sealed(sa, self.bx, M.new_msg_id(), c["ed"])
        t = bytearray(sa); t[-3] ^= 1
        with self.assertRaises(M.MailError):
            M.open_sealed(bytes(t), self.bx, a["msg_id"], c["ed"])

    def test_forged_sender_rejected(self):
        c = self.bcard["card"]
        ek, ex = ident()  # Eve claims to be Alice
        m = M.make_message(ek, ex, ["http://x"], c, "from alice", "trust me")
        m["from"] = F.pub_hex(self.ak)
        with self.assertRaises(M.MailError):
            M.open_sealed(M.seal(m, c["x"]), self.bx, m["msg_id"], c["ed"])
        m2 = M.make_message(self.ak, self.ax, ["http://x"], c, "s", "original")
        m2["body"] = "altered"
        with self.assertRaises(M.MailError):
            M.open_sealed(M.seal(m2, c["x"]), self.bx, m2["msg_id"], c["ed"])

    def test_contact_tag_symmetric_and_per_host(self):
        a_side = M.contact_tag(self.ax, FC.pub_hex(self.bx), "b", "h1")
        b_side = M.contact_tag(self.bx, FC.pub_hex(self.ax), "b", "h1")
        self.assertEqual(a_side, b_side)
        self.assertNotEqual(a_side, M.contact_tag(self.ax, FC.pub_hex(self.bx), "b", "h2"))

    def test_too_long(self):
        with self.assertRaises(M.MailError):
            M.make_message(self.ak, self.ax, ["http://x"], self.bcard["card"], "s", "x" * 70000)


class HostUnit(unittest.TestCase):
    def setUp(self):
        self.t = tmpdir()
        self.now = [1_800_000_000.0]
        self.h = MH.Host(self.t, {"requests_max_msgs": 3, "requests_per_hour": 100},
                         clock=lambda: self.now[0])
        self.bk, self.bx = ident()
        self.b = F.pub_hex(self.bk)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def auth(self, key, op, args=None):
        _, c = self.h.challenge({"to": F.pub_hex(key)})
        return self.h.auth(M.sign_auth(key, self.h.id, op, c["nonce"], args or {}))

    def put(self, n=200, ttl=None, tag=None):
        r = {"to": self.b, "msg_id": M.new_msg_id(), "blob": M.b64e(M.MAGIC + os.urandom(n))}
        if ttl:
            r["ttl"] = ttl
        if tag:
            r["tag"] = tag
        return self.h.put(r)

    def test_unregistered_refused_then_registered(self):
        self.assertEqual(self.put()[0], 404)
        self.assertEqual(self.auth(self.bk, "register")[0], 200)
        self.assertEqual(self.put()[1]["pile"], "requests")

    def test_auth_rules(self):
        self.auth(self.bk, "register")
        self.put()
        ek, _ = ident()
        _, c = self.h.challenge({"to": self.b})  # Eve signs Bob's challenge with her key
        r = M.sign_auth(ek, self.h.id, "list", c["nonce"], {})
        r["to"] = self.b
        self.assertEqual(self.h.auth(r)[0], 401)
        _, c = self.h.challenge({"to": self.b})
        good = M.sign_auth(self.bk, self.h.id, "list", c["nonce"], {})
        self.assertEqual(self.h.auth(good)[0], 200)
        self.assertEqual(self.h.auth(good)[0], 401)  # nonce single use
        _, c = self.h.challenge({"to": self.b})
        other = M.sign_auth(self.bk, "ab" * 32, "list", c["nonce"], {})  # signed for another host
        self.assertEqual(self.h.auth(other)[0], 401)
        _, c = self.h.challenge({"to": self.b})
        self.now[0] += 121
        self.assertEqual(self.h.auth(M.sign_auth(self.bk, self.h.id, "list", c["nonce"], {}))[0], 401)

    def test_piles_quota_size_ttl(self):
        self.auth(self.bk, "register")
        self.auth(self.bk, "tags", {"tags": ["aa" * 16]})
        self.assertEqual(self.put(tag="aa" * 16)[1]["pile"], "inbox")
        for _ in range(3):
            self.assertEqual(self.put()[0], 200)
        self.assertEqual(self.put()[1]["error"], "quota")
        self.assertEqual(self.put(n=20000, tag="aa" * 16)[0], 200)  # big ok in inbox
        self.assertEqual(self.put(n=M.MAX_SEALED)[0], 413)
        code, r = self.put(tag="aa" * 16, ttl=90 * 86400)
        lst = self.auth(self.bk, "list")[1]["msgs"]
        newest = max(lst, key=lambda m: m["exp"])
        self.assertEqual(newest["exp"] - newest["ts"], 30 * 86400)  # TTL clamped to 30 d
        self.assertEqual(min(m["exp"] - m["ts"] for m in lst), 7 * 86400)
        self.now[0] += 8 * 86400
        self.assertEqual(self.h.purge(), len(lst) - 1)

    def test_fetch_keeps_until_ack(self):
        self.auth(self.bk, "register")
        self.put()
        ids = [m["msg_id"] for m in self.auth(self.bk, "list")[1]["msgs"]]
        self.assertEqual(len(self.auth(self.bk, "fetch", {"ids": ids})[1]["msgs"]), 1)
        self.assertEqual(len(self.auth(self.bk, "list")[1]["msgs"]), 1)  # still there
        self.assertEqual(self.auth(self.bk, "ack", {"ids": ids})[1]["deleted"], 1)
        self.assertEqual(self.auth(self.bk, "list")[1]["msgs"], [])
        self.put()  # unacked mail goes at TTL
        self.now[0] += 7 * 86400 + 1
        self.assertEqual(self.h.purge(), 1)

    def test_stamp_hook(self):
        self.auth(self.bk, "register")
        self.h.stamp_check = lambda stamp, to, pile: None if stamp == "paid" else "stamp required"
        self.assertEqual(self.put()[0], 402)


class E2E(unittest.TestCase):
    """Three local mail hosts. Alice uses h1; Bob lists h2+h3. Real HTTP, real crypto."""

    def setUp(self):
        self.t = tmpdir()
        self.hosts, self.srvs, self.urls = [], [], []
        for i in range(3):
            h = MH.Host(os.path.join(self.t, f"h{i}"))
            s = MH.serve(h, purge=False)
            self.hosts.append(h)
            self.srvs.append(s)
            self.urls.append(f"http://127.0.0.1:{s.server_address[1]}")
        self.alice = MailClient(os.path.join(self.t, "alice"), os.urandom(32), [self.urls[0]])
        self.bob = MailClient(os.path.join(self.t, "bob"), os.urandom(32), self.urls[1:])
        self.alice.register()
        self.bob.register()

    def tearDown(self):
        for s in self.srvs:
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.t, ignore_errors=True)

    def no_plaintext_on_disk(self, needles):
        for root, _, files in os.walk(self.t):
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    data = fh.read()
                for n in needles:
                    self.assertNotIn(n.encode(), data, f"plaintext in {os.path.join(root, f)}")

    def test_flow(self):
        bob_link = self.bob.link("Bob")
        r = self.alice.send(bob_link, "Plans", SECRET)
        self.assertTrue(r["ok"])
        self.assertEqual(set(r["hosts"].values()), {"requests"})  # stranger -> requests on both hosts
        f = self.bob.fetch()
        self.assertEqual((f["new"], f["dups"], f["rejected"]), (1, 1, []))  # deduped across hosts
        reqs = self.bob.folder("requests")
        self.assertEqual(reqs[0]["subject"], "Plans")
        self.assertEqual(self.bob.open(reqs[0]["msg_id"])["body"], SECRET)
        # Bob accepts Alice -> later mail from her lands in the host inbox pile
        self.bob.accept_sender(reqs[0]["msg_id"])
        self.assertEqual(len(self.bob.folder("inbox")), 1)
        r2 = self.alice.send(bob_link, "Again", SECRET + " v2")
        self.assertEqual(set(r2["hosts"].values()), {"inbox"})
        # Bob replies; Alice gets it, verified, as a request (she hasn't accepted Bob)
        self.bob.fetch()
        self.bob.reply(reqs[0]["msg_id"], "reply " + SECRET)
        self.alice.fetch()
        got = self.alice.folder("requests")
        self.assertEqual(self.alice.open(got[0]["msg_id"])["body"], "reply " + SECRET)
        # attachments are encrypted file-drop links carried inside the sealed body
        blob, key = FC.encrypt(b"attached!", "a.txt", "text/plain")
        att = FC.link("http://files.example", FC.sha256(blob), key)
        self.alice.add_contact(bob_link)
        self.alice.send("Bob", "file", "see attached", [att])
        self.bob.fetch()
        m = [x for x in self.bob.folder("inbox") if x["subject"] == "file"][0]
        self.assertEqual(self.bob.open(m["msg_id"])["attach"], [att])
        self.no_plaintext_on_disk([SECRET, "Plans", "see attached", "attached!"])

    def test_crash_before_ack_keeps_mail(self):
        self.alice.send(self.bob.link(), "s", SECRET)
        real = self.bob._auth

        def no_ack(h, op, args=None):
            if op == "ack":
                raise OSError("client died before ack")
            return real(h, op, args)
        self.bob._auth = no_ack
        f = self.bob.fetch()
        self.assertEqual(f["new"], 1)  # saved locally
        self.assertTrue(f["errors"])
        for h in self.urls[1:]:  # host still holds it
            self.assertEqual(len(real(h, "list")["msgs"]), 1)
        self.bob._auth = real
        f = self.bob.fetch()  # retried: deduped, then acked
        self.assertEqual((f["new"], f["errors"]), (0, {}))
        for h in self.urls[1:]:
            self.assertEqual(real(h, "list")["msgs"], [])

    def test_one_host_down_still_delivers(self):
        link = self.bob.link()
        self.srvs[1].shutdown(); self.srvs[1].server_close()
        r = self.alice.send(link, "s", SECRET)
        self.assertTrue(r["ok"])
        f = self.bob.fetch()
        self.assertEqual(f["new"], 1)
        self.assertIn(self.urls[1], f["errors"])

    def test_forgery_rejected_end_to_end(self):
        link = self.bob.link()
        env, _ = M.parse_link(link)
        c = env["card"]
        eve_k, eve_x = ident()
        m = M.make_message(eve_k, eve_x, [self.urls[0]], c, "from alice", SECRET)
        m["from"] = self.alice.me  # forge Alice as the sender
        blob = M.seal(m, c["x"])
        for u in c["hosts"]:
            http_json(u + "/v1/mail/put", {"to": c["ed"], "msg_id": m["msg_id"], "blob": M.b64e(blob)})
        f = self.bob.fetch()
        self.assertEqual(f["new"], 0)
        self.assertTrue(f["rejected"] and "signature" in f["rejected"][0]["why"])
        self.assertEqual(self.bob.folder("requests") + self.bob.folder("inbox"), [])

    def test_unauthorized_inbox_read_rejected(self):
        self.alice.send(self.bob.link(), "s", SECRET)
        u = self.urls[1]
        eve_k, _ = ident()
        _, c = http_json(u + "/v1/mail/challenge", {"to": self.bob.me})
        req = M.sign_auth(eve_k, c["host"], "fetch", c["nonce"], {"ids": []})
        req["to"] = self.bob.me
        code, r = http_json(u + "/v1/mail/auth", req)
        self.assertEqual(code, 401)
        _, c = http_json(u + "/v1/mail/challenge", {"to": self.bob.me})  # no signature at all
        code, _ = http_json(u + "/v1/mail/auth", {"to": self.bob.me, "op": "list", "nonce": c["nonce"]})
        self.assertEqual(code, 401)
        code, _ = http_json(u + "/v1/mail/auth", {"to": self.bob.me, "op": "list", "nonce": "00" * 16,
                                                  "sig": "00" * 64})
        self.assertEqual(code, 401)
        self.assertEqual(self.bob.fetch()["new"], 1)  # Bob's mail is still there

    def test_stranger_cannot_fake_contact_tag(self):
        self.bob.add_contact(self.alice.link())
        eve = MailClient(os.path.join(self.t, "eve"), os.urandom(32), [self.urls[0]])
        r = eve.send(self.bob.link(), "hi", "spam")
        self.assertEqual(set(r["hosts"].values()), {"requests"})
        r = self.alice.send(self.bob.link(), "hi", "real")
        self.assertEqual(set(r["hosts"].values()), {"inbox"})

    def test_petnames(self):
        r = self.alice.petname("srnet://room/abc123?name=Bobs%20Chat%20Room")
        self.assertEqual(r, {"key": "srnet://room/abc123", "name": "Bobs Chat Room"})
        self.alice.petname("srnet://room/abc123?name=Something%20Else")  # suggestion doesn't overwrite
        self.assertEqual(self.alice.petname("srnet://room/abc123")["name"], "Bobs Chat Room")
        self.alice.petname("srnet://room/abc123", "Bob's room")
        self.assertEqual(self.alice.petname("srnet://room/abc123")["name"], "Bob's room")
        self.alice.petname("srnet://room/abc123", "")
        self.assertIsNone(self.alice.petname("srnet://room/abc123")["name"])


if __name__ == "__main__":
    unittest.main()


class CoreAPI(unittest.TestCase):
    def test_mail_page_and_api(self):
        import urllib.request
        from typed.core import Core, serve
        t = tmpdir()
        h = MH.Host(os.path.join(t, "h"))
        hs = MH.serve(h, purge=False)
        url = f"http://127.0.0.1:{hs.server_address[1]}"
        core = Core(os.path.join(t, "c"), {"mail": {"hosts": [url]}})
        srv = serve(core, sync_every=0)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            page = urllib.request.urlopen(base + "/mail").read()
            self.assertIn(b"mail.js", page)
            req = urllib.request.Request(base + "/api/mail/register", data=b"{}",
                                         headers={"X-Core": "1", "Content-Type": "application/json"})
            self.assertEqual(json.loads(urllib.request.urlopen(req).read())[url], "ok")
            req = urllib.request.Request(base + "/api/mail/summary", data=b"{}",
                                         headers={"X-Core": "1", "Content-Type": "application/json"})
            s = json.loads(urllib.request.urlopen(req).read())
            self.assertEqual(s["me"], core.me)  # mail identity == posting identity
            self.assertTrue(s["link"].startswith("srnet://mail/" + core.me))
            req = urllib.request.Request(base + "/api/mail/summary", data=b"{}")  # no X-Core
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(req)
        finally:
            for s_ in (srv, hs):
                s_.shutdown(); s_.server_close()
            shutil.rmtree(t, ignore_errors=True)


class OneClickAttach(unittest.TestCase):
    def test_attach_uploads_to_link_drop(self):
        from typed import testbed as T
        from typed.core import Core
        env = T.start(mirror=False)
        t = tmpdir()
        hs = MH.serve(MH.Host(os.path.join(t, "h")), purge=False)
        try:
            cfg = dict(T.client_config(env), mail={"hosts": [f"http://127.0.0.1:{hs.server_address[1]}"]})
            a = Core(os.path.join(t, "a"), cfg, autoconfirm=True)
            b = Core(os.path.join(t, "b"), cfg, autoconfirm=True)
            a.mail.register(); b.mail.register()
            secret = b"ATTACHED-" + SECRET.encode()
            link = a.mail.api("attach", {"data": M.b64e(secret), "name": "plan.txt", "mime": "text/plain"})["link"]
            self.assertIn("#k=", link)
            a.mail.send(b.mail.link(), "with file", "see file", [link])
            b.mail.fetch()
            m = b.mail.open(b.mail.folder("requests")[0]["msg_id"])
            data, name, _ = b.open_link(m["attach"][0])
            self.assertEqual((data, name), (secret, "plan.txt"))
            for top in (str(env["tmp"]), t):
                for root, _, files in os.walk(top):
                    for f in files:
                        with open(os.path.join(root, f), "rb") as fh:
                            self.assertNotIn(b"ATTACHED-", fh.read(), os.path.join(root, f))
        finally:
            hs.shutdown(); hs.server_close()
            T.stop(env)
            shutil.rmtree(t, ignore_errors=True)
