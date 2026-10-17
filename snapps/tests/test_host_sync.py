import json, sqlite3, time, unittest
from helpers import F, H, start_host, tmpdir
from mbclient.sync import SyncError, http_json, submit, sync_once
from mbhost.store import Store
from mbhost.mirror import Mirror
from mbhost.ratelimit import RateLimiter


class HostSync(unittest.TestCase):
    def setUp(self):
        self.h, self.srv, self.url, self.root = start_host(tmpdir())
        self.k = F.new_key()

    def tearDown(self):
        self.srv.shutdown()

    def post(self, body, **kw):
        return submit([self.url], F.make_post(self.k, "demo", body, pow_bits=8, **kw))

    def fresh_client(self):
        return Store(tmpdir() + "/c.sqlite3"), F.CheckpointPin()

    def test_submit_and_sync_incremental(self):
        a = self.post("one")
        self.assertTrue(a["ok"])
        self.post("two", reply_to=a["id"])
        st, pin = self.fresh_client()
        r = sync_once(st, [self.url], self.root, "demo", pin)
        self.assertEqual((r["added"], r["seq"]), (2, 2))
        self.post("three")
        r = sync_once(st, [self.url], self.root, "demo", pin)
        self.assertEqual((r["added"], st.head()[0]), (1, 3))
        _, t = http_json(self.url + f"/v1/thread/{a['id']}")
        self.assertEqual(len(t["replies"]), 1)

    def test_paging(self):
        for i in range(7):
            self.post(f"p{i}")
        _, pg = http_json(self.url + "/v1/log?since=2&limit=3")
        self.assertEqual([e["seq"] for e in pg["entries"]], [3, 4, 5])

    def test_replay_is_idempotent_and_cross_board_refused(self):
        e = F.make_post(self.k, "demo", "once", pow_bits=8)
        r1, r2 = submit([self.url], e), submit([self.url], e)
        self.assertTrue(r2.get("dup"))
        self.assertEqual(r1["seq"], r2["seq"])
        self.assertEqual(self.h.store.head()[0], 1)
        other = F.make_post(self.k, "otherboard", "x", pow_bits=8)
        self.assertEqual(submit([self.url], other)["status"], 400)

    def test_stale_and_future_timestamps_refused(self):
        for ts in (int(time.time()) - 3600, int(time.time()) + 3600):
            r = submit([self.url], F.make_post(self.k, "demo", "t", ts=ts, pow_bits=8))
            self.assertEqual(r["status"], 400, ts)

    def test_pow_enforced(self):
        r = submit([self.url], F.make_post(self.k, "demo", "no work", pow_bits=0))
        self.assertEqual(r["status"], 400)
        self.assertIn("proof of work", r["error"])

    def test_tampered_submission_refused(self):
        e = F.make_post(self.k, "demo", "real", pow_bits=8)
        e["post"]["body"] = "fake"
        self.assertEqual(submit([self.url], e)["status"], 400)

    def test_forged_host_edit_rejected_by_client(self):
        a = self.post("I never said this")
        self.post("second")
        # host operator edits a stored post body (even re-hashing the chain cannot fix the sig)
        env = self.h.store.get(a["id"])["env"]
        env["post"]["body"] = "edited by host"
        self.h.store.db.execute("UPDATE log SET env=? WHERE id=?", (json.dumps(env), a["id"]))
        st, pin = self.fresh_client()
        with self.assertRaises(SyncError):
            sync_once(st, [self.url], self.root, "demo", pin)
        self.assertEqual(st.head()[0], 0)  # nothing committed

    def test_host_dropping_a_post_detected(self):
        self.post("a"); b = self.post("b"); self.post("c")
        self.h.store.db.execute("DELETE FROM log WHERE id=?", (b["id"],))
        st, pin = self.fresh_client()
        with self.assertRaises(SyncError):
            sync_once(st, [self.url], self.root, "demo", pin)

    def test_rollback_and_fork_detected(self):
        self.post("a"); self.post("b")
        st, pin = self.fresh_client()
        sync_once(st, [self.url], self.root, "demo", pin)
        # host rewrites history at seq 2 with a different valid post (fork)
        self.h.store.db.execute("DELETE FROM log WHERE seq=2")
        self.post("b-prime"); self.post("c")
        with self.assertRaises(SyncError):
            sync_once(st, [self.url], self.root, "demo", pin)
        # rollback: checkpoint lower than pinned
        pin2 = F.CheckpointPin(99, "f" * 64)
        with self.assertRaises(SyncError):
            sync_once(Store(tmpdir() + "/x"), [self.url], self.root, "demo", pin2)

    def test_wrong_root_pin(self):
        self.post("a")
        st, pin = self.fresh_client()
        with self.assertRaises(SyncError):
            sync_once(st, [self.url], F.pub_hex(F.new_key()), "demo", pin)

    def test_failover_and_mirror(self):
        for i in range(3):
            self.post(f"m{i}")
        m = Mirror(tmpdir(), "demo", self.root, [self.url])
        self.assertEqual(m.pull()["added"], 3)
        msrv = H.serve(m)
        murl = f"http://127.0.0.1:{msrv.server_address[1]}"
        try:
            dead = "http://127.0.0.1:9"  # nothing listening
            st, pin = self.fresh_client()
            r = sync_once(st, [dead, murl], self.root, "demo", pin)
            self.assertEqual((r["host"], r["added"]), (murl, 3))
            self.assertIn(dead, r["errors"])
            # a mirror that tampers is skipped in favour of an honest host
            env = m.store.since(0)[0]["env"]; env["post"]["body"] = "mirror lie"
            m.store.db.execute("UPDATE log SET env=? WHERE seq=1", (json.dumps(env),))
            st, pin = self.fresh_client()
            r = sync_once(st, [murl, self.url], self.root, "demo", pin)
            self.assertEqual(r["host"], self.url)
            r = submit([murl], F.make_post(self.k, "demo", "via mirror", pow_bits=8))
            self.assertTrue(r["ok"] and r["forwarded"])
        finally:
            msrv.shutdown()

    def test_host_serves_json_only(self):
        import urllib.request
        for path in ("/v1/info", "/v1/checkpoint", "/v1/log", "/v1/nope"):
            try:
                r = urllib.request.urlopen(self.url + path)
            except urllib.error.HTTPError as e:
                r = e
            self.assertTrue(r.headers["Content-Type"].startswith("application/json"), path)
            self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_public_bind_refused(self):
        self.assertFalse(H.bind_ok("0.0.0.0"))
        self.assertTrue(H.bind_ok("127.0.0.1"))


class Rate(unittest.TestCase):
    def test_limiter_window(self):
        t = [0.0]
        rl = RateLimiter([(3, 10)], clock=lambda: t[0])
        self.assertEqual([rl.allow("k") for _ in range(4)], [True, True, True, False])
        self.assertTrue(rl.allow("other"))
        t[0] = 10.5
        self.assertTrue(rl.allow("k"))

    def test_host_rate_limit(self):
        h, srv, url, _ = start_host(tmpdir(), rate=[[2, 3600]])
        try:
            k = F.new_key()
            rs = [submit([url], F.make_post(k, "demo", f"r{i}", pow_bits=8)) for i in range(3)]
            self.assertEqual([r.get("ok") for r in rs], [True, True, False])
            self.assertEqual(rs[2]["status"], 429)
            self.assertTrue(submit([url], F.make_post(F.new_key(), "demo", "other key", pow_bits=8))["ok"])
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
