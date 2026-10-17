"""Mirror push/forwarding, failover with host killed, moderation, new-key PoW, retention,
deletes, outbox, IPv6 bind."""
import json, socket, time, unittest
from helpers import F, H, serve, url_of, start_host, tmpdir
from mbclient.client import Client
from mbclient.sync import SyncError, http_json, submit, sync_once
from mbhost.mirror import Mirror
from mbhost.node import bind_ok
from mbhost.store import Store


def fresh():
    return Store(tmpdir() + "/c.sqlite3"), F.CheckpointPin()


class Base(unittest.TestCase):
    def setUp(self):
        self.servers = []
        self.h, s, self.url, self.root = start_host(tmpdir())
        self.servers.append(s)
        self.k = F.new_key()

    def tearDown(self):
        for s in self.servers:
            try:
                s.shutdown(); s.server_close()
            except Exception:
                pass

    def post(self, body, key=None, **kw):
        return submit([self.url], F.make_post(key or self.k, "demo", body, pow_bits=8, **kw))

    def mirror(self):
        m = Mirror(tmpdir(), "demo", self.root, [self.url])
        s = serve(m); self.servers.append(s)
        return m, s, url_of(s)


class MirrorPush(Base):
    def test_push_and_forward(self):
        m, ms, murl = self.mirror()
        self.h.mirrors = [murl]
        a = self.post("pushed 1"); self.post("pushed 2", reply_to=a["id"])
        self.h.moderate("hide", "post", a["id"], "test")
        rep = self.h.push()
        self.assertTrue(rep[murl]["ok"], rep)
        self.assertEqual(m.store.head(), self.h.store.head())
        self.assertEqual(m.store.rhead(), 1)
        # mirror serves the primary's checkpoint; a client verifies it like the primary's
        st, pin = fresh()
        r = sync_once(st, [murl], self.root, "demo", pin)
        self.assertEqual((r["added"], r["records"]), (2, 1))
        # submit to the mirror is forwarded to the primary
        r = submit([murl], F.make_post(self.k, "demo", "to mirror", pow_bits=8))
        self.assertTrue(r["ok"] and r["forwarded"])
        self.assertEqual(self.h.store.head()[0], 3)

    def test_push_with_many_entries_uses_intermediate_checkpoints(self):
        m, ms, murl = self.mirror()
        self.h.mirrors = [murl]
        ks = [F.new_key() for _ in range(3)]
        for i in range(12):
            self.post(f"n{i}", key=ks[i % 3])
        import mbhost.host as HH
        orig = self.h.store.since
        self.h.store.since = lambda s, lim=500: orig(s, 5)  # force small pages
        try:
            self.assertTrue(self.h.push()[murl]["ok"])
        finally:
            self.h.store.since = orig
        self.assertEqual(m.store.head(), self.h.store.head())

    def test_mirror_rejects_forged_publish(self):
        m, ms, murl = self.mirror()
        self.post("real")
        cp = self.h.checkpoint()
        ents = self.h.store.since(0)
        ents[0]["env"]["post"]["body"] = "forged"
        code, r = http_json(murl + "/v1/publish", {"delegation": self.h.delegation,
                            "checkpoint": cp, "policy": self.h.policy, "entries": ents,
                            "records": []})
        self.assertEqual(code, 400)
        # wrong key signs a checkpoint: rejected (not delegated)
        evil = F.new_key()
        bad_cp = F.make_checkpoint(evil, "demo", 1, self.h.store.head()[1])
        code, _ = http_json(murl + "/v1/publish", {"delegation": self.h.delegation,
                            "checkpoint": bad_cp, "policy": self.h.policy,
                            "entries": self.h.store.since(0), "records": []})
        self.assertEqual(code, 400)
        self.assertEqual(m.store.head()[0], 0)

    def test_dead_mirror_does_not_stop_push(self):
        m, ms, murl = self.mirror()
        self.h.mirrors = ["http://127.0.0.1:9", murl]
        self.post("x")
        rep = self.h.push()
        self.assertFalse(rep["http://127.0.0.1:9"]["ok"])
        self.assertTrue(rep[murl]["ok"])


class FailoverOutbox(Base):
    def test_kill_host_mid_demo(self):
        m, ms, murl = self.mirror()
        self.h.mirrors = [murl]
        alice = Client(tmpdir(), "demo", self.root, [self.url, murl])
        bob = Client(tmpdir(), "demo", self.root, [self.url, murl])
        a = alice.post("before the crash")
        self.assertTrue(a["ok"])
        self.h.push()
        # kill the primary
        self.servers[0].shutdown(); self.servers[0].server_close()
        r = bob.sync()
        self.assertIsNone(r["error"]); self.assertEqual(r["host"], murl)
        self.assertEqual(bob.thread(a["id"])["root"]["body"], "before the crash")
        # bob replies: mirror cannot forward (503), post goes to the outbox
        r = bob.post("reply while primary is down", a["id"])
        self.assertTrue(r.get("queued"), r)
        self.assertEqual(bob.state()["outbox"], 1)
        # primary comes back on the same port; next sync flushes the outbox
        port = int(self.url.rsplit(":", 1)[1])
        self.servers[0] = serve(self.h, "127.0.0.1", port)
        bob.sync()
        self.assertEqual(bob.state()["outbox"], 0)
        alice.sync()
        self.assertEqual([x["body"] for x in alice.thread(a["id"])["replies"]],
                         ["reply while primary is down"])

    def test_outbox_when_nothing_reachable(self):
        c = Client(tmpdir(), "demo", self.root, ["http://127.0.0.1:9"])
        self.assertTrue(c.post("offline")["queued"])
        self.assertEqual(c.post("x" * 501)["ok"], False)  # invalid never queued
        self.assertEqual(c.state()["outbox"], 1)


class Moderation(Base):
    def test_hide_honoured_with_local_override(self):
        bad = F.new_key()
        a = self.post("ok post"); b = self.post("spam spam", key=bad)
        c = Client(tmpdir(), "demo", self.root, [self.url])
        c.sync()
        self.assertEqual(len(c.timeline()), 2)
        self.h.moderate("hide", "post", b["id"], "spam")
        c.sync()
        self.assertEqual([p["body"] for p in c.timeline()], ["ok post"])
        c.reveal(b["id"])
        self.assertEqual(len(c.timeline()), 2)
        c.reveal(b["id"], False)
        self.h.moderate("hide", "author", F.pub_hex(bad), "spammer")
        c.sync()
        self.assertEqual(len(c.timeline()), 1)
        c.set_moderation("ignore")
        self.assertEqual(len(c.timeline()), 2)
        c.set_moderation("honour")
        self.h.moderate("unhide", "author", F.pub_hex(bad), "appeal ok")
        self.h.moderate("unhide", "post", b["id"], "appeal ok")
        c.sync()
        self.assertEqual(len(c.timeline()), 2)

    def test_forged_mod_record_rejected(self):
        self.post("x")
        self.h.moderate("hide", "post", self.h.store.since(0)[0]["id"], "r")
        rec = json.loads(self.h.store.db.execute("SELECT rec FROM records").fetchone()[0])
        rec["mod"]["reason"] = "edited"
        self.h.store.db.execute("UPDATE records SET rec=?", (json.dumps(rec),))
        st, pin = fresh()
        with self.assertRaises(SyncError):
            sync_once(st, [self.url], self.root, "demo", pin)
        # a mod record signed by a non-delegated key
        with self.assertRaises(F.FormatError):
            F.verify_mod(F.make_mod(F.new_key(), "demo", "hide", "post", "y" * 26, ""),
                         F.pub_hex(self.h.key), "demo")

    def test_policy_served_and_bound_to_checkpoint(self):
        _, pol = http_json(self.url + "/v1/policy")
        cp = self.h.checkpoint()["checkpoint"]
        self.assertEqual(F.policy_hash(pol), cp["policy"])
        self.assertIn("rules", pol)
        self.h.policy["rules"] = "changed after signing"  # served policy no longer matches
        self.h._cp = None
        cp_old = F.make_checkpoint(self.h.key, "demo", 0, F.GENESIS,
                                   policy=F.policy_hash(dict(pol)))
        self.h.checkpoint = lambda: cp_old
        st, pin = fresh()
        with self.assertRaises(SyncError):
            sync_once(st, [self.url], self.root, "demo", pin)


class NewKeyPow(unittest.TestCase):
    def test_new_keys_need_more_work(self):
        t = [time.time()]
        h, srv, url, root = start_host(tmpdir(), pow_bits=6, new_key_pow_bits=14,
                                       new_key_age_s=3600, clock=lambda: t[0])
        try:
            k = F.new_key()
            r = submit([url], F.make_post(k, "demo", "low work", pow_bits=6, ts=int(t[0])))
            self.assertEqual(r["status"], 400)
            self.assertTrue(submit([url], F.make_post(k, "demo", "hi", pow_bits=14, ts=int(t[0])))["ok"])
            # still new within the age window
            self.assertEqual(submit([url], F.make_post(k, "demo", "2", pow_bits=6, ts=int(t[0])))["status"], 400)
            t[0] += 3700  # key is now established
            self.assertTrue(submit([url], F.make_post(k, "demo", "3", pow_bits=6, ts=int(t[0])))["ok"])
            # client picks the higher figure for a key it has never posted with
            c = Client(tmpdir(), "demo", root, [url]); c.sync()
            self.assertEqual(c._pow_bits(), 14)
        finally:
            srv.shutdown(); srv.server_close()

    def test_new_key_rate_limit(self):
        h, srv, url, root = start_host(tmpdir(), new_key_rate=[[2, 3600]])
        try:
            k = F.new_key()
            rs = [submit([url], F.make_post(k, "demo", f"r{i}", pow_bits=8)) for i in range(3)]
            self.assertEqual(rs[2]["status"], 429)
            self.assertIn("new key", rs[2]["error"])
        finally:
            srv.shutdown(); srv.server_close()


class RetentionDelete(Base):
    def test_author_delete(self):
        a = self.post("regret this"); b = self.post("keep")
        c = Client(tmpdir(), "demo", self.root, [self.url]); c.sync()
        # someone else cannot delete it
        r = submit([self.url], F.make_delete(F.new_key(), "demo", [a["id"]]), "/v1/delete")
        self.assertEqual(r["status"], 400)
        r = submit([self.url], F.make_delete(self.k, "demo", [a["id"]]), "/v1/delete")
        self.assertTrue(r["ok"])
        self.assertIn("tomb", self.h.store.get(a["id"]))
        c.sync()  # existing client redacts its cached copy
        self.assertEqual([p["body"] for p in c.timeline()], ["keep"])
        st, pin = fresh()  # a new client verifies the chain through the tombstone
        r = sync_once(st, [self.url], self.root, "demo", pin)
        self.assertEqual(r["added"], 2)

    def test_client_delete_api(self):
        c = Client(tmpdir(), "demo", self.root, [self.url])
        p = c.post("mine")
        self.assertTrue(c.delete(p["id"])["ok"])
        self.assertEqual(c.timeline(), [])
        other = self.post("not mine")
        c.sync()
        self.assertFalse(c.delete(other["id"])["ok"])

    def test_forged_tombstone_rejected(self):
        a = self.post("innocent"); self.post("other")
        # host drops a post's text and claims a delete record from someone else
        fake = F.make_delete(F.new_key(), "demo", ["y" * 26])
        self.h.store.redact(a["id"], {"by": "delete", "record": fake})
        st, pin = fresh()
        with self.assertRaises(SyncError):
            sync_once(st, [self.url], self.root, "demo", pin)

    def test_retention(self):
        t = [time.time()]
        h, srv, url, root = start_host(tmpdir(), retention_days=1, clock=lambda: t[0])
        try:
            k = F.new_key()
            old = submit([url], F.make_post(k, "demo", "old", pow_bits=8, ts=int(t[0])))
            t[0] += 2 * 86400
            submit([url], F.make_post(k, "demo", "new", pow_bits=8, ts=int(t[0])))
            self.assertEqual(h.apply_retention(), 1)
            self.assertEqual(h.store.get(old["id"])["tomb"], {"by": "retention"})
            st, pin = fresh()
            sync_once(st, [url], root, "demo", pin, now=t[0])
            self.assertEqual(st.head(), h.store.head())
        finally:
            srv.shutdown(); srv.server_close()


class IPv6(unittest.TestCase):
    def test_bind_rules(self):
        for a in ("127.0.0.1", "::1", "[::1]", "10.198.0.1", "fd2e:7365:7368::1", "localhost"):
            self.assertTrue(bind_ok(a), a)
        for a in ("0.0.0.0", "::", "8.8.8.8", "2001:db8::1", "9.9.9.9"):
            self.assertFalse(bind_ok(a), a)

    def test_serve_on_ipv6_loopback(self):
        if not socket.has_ipv6:
            self.skipTest("no IPv6")
        h, s4, url4, root = start_host(tmpdir())
        s6 = serve(h, "::1", 0)
        try:
            u6 = url_of(s6)
            self.assertTrue(u6.startswith("http://[::1]:"))
            submit([u6], F.make_post(F.new_key(), "demo", "over v6", pow_bits=8))
            st, pin = fresh()
            self.assertEqual(sync_once(st, [u6], root, "demo", pin)["added"], 1)
        finally:
            for s in (s4, s6):
                s.shutdown(); s.server_close()


if __name__ == "__main__":
    unittest.main()
