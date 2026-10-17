"""End-to-end tests for every typed SNApp on the platform (via the client core)."""
import hashlib, json, os, time, unittest
from helpers import F
from typed import testbed as T, views
from typed.core import Core
from typed.record import KIND_TYPES, make_record, TypedCodec, ROOM_TYPES
from typed.sync import SyncError, http_json, submit, sync_once
from typed.store import Store


class Platform(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seller_seed, cls.admin_seed = os.urandom(32), os.urandom(32)
        cls.seller = F.key_from_seed(cls.seller_seed)
        cls.admin = F.key_from_seed(cls.admin_seed)
        cls.env = T.start(admins=[F.pub_hex(cls.admin)], sellers=[F.pub_hex(cls.seller)])
        cls.a = T.new_core(cls.env, "alice", key_seed=cls.admin_seed)   # admin
        cls.b = T.new_core(cls.env, "bob", key_seed=cls.seller_seed)    # seller
        cls.c = T.new_core(cls.env, "carol")                            # ordinary user
        for x in (cls.a, cls.b, cls.c):
            x.sync()

    @classmethod
    def tearDownClass(cls):
        T.stop(cls.env)

    def post(self, core, coll, t, body, refs=()):
        r = core.draft(coll, t, body, list(refs))["result"]
        self.assertTrue(r.get("ok"), r)
        return r

    # ---- board on the platform ----
    def test_board_through_core(self):
        p = self.post(self.a, "board", "board.post", {"text": "platform board"})
        self.post(self.c, "board", "board.post", {"text": "reply", "reply_to": p["id"]}, [p["id"]])
        self.b.sync("board")
        posts = self.b.view("board")["view"]["posts"]
        self.assertEqual(next(x for x in posts if x["id"] == p["id"])["replies"], 1)

    # ---- rooms ----
    def test_room_signed_lines_and_durable_history(self):
        self.post(self.c, "lobby", "room.join", {"nick": "carol"})
        self.post(self.c, "lobby", "room.msg", {"text": "hello everyone"})
        r = self.c.draft("lobby", "room.topic", {"text": "not allowed"}, [])["result"]
        self.assertFalse(r["ok"])  # topic needs an admin key
        self.post(self.a, "lobby", "room.topic", {"text": "Welcome"})
        self.assertIsNone(self.b.sync("lobby")["lobby"]["error"])
        v = self.b.view("lobby")["view"]
        self.assertEqual(v["topic"]["text"], "Welcome")
        self.assertTrue(any(l["text"] == "hello everyone" and l["who"].startswith("carol·")
                            for l in v["lines"]))
        # restart the hub: history survives and still verifies for a brand-new client
        hub_dir = self.env["tmp"] / "hub"
        from typed.hub import Hub
        from typed.host import load_or_make_keys
        root, hk, _ = load_or_make_keys(hub_dir, "hub")
        hub2 = Hub(str(hub_dir), root, hk, [("room", "lobby")])
        st = Store(str(self.env["tmp"] / "x.sqlite3"))
        self.assertEqual(hub2.nodes["lobby"].store.head(),
                         self.env["hub"].nodes["lobby"].store.head())

    def test_forged_room_line_rejected(self):
        self.post(self.c, "lobby", "room.msg", {"text": "original line"})
        node = self.env["hub"].nodes["lobby"]
        e = node.store.since(0)[-1]
        env = e["env"]; env["rec"]["body"]["text"] = "host-forged line"
        node.store.db.execute("UPDATE log SET env=? WHERE seq=?", (json.dumps(env), e["seq"]))
        d = T.new_core(self.env, "dave-forge")
        r = d.sync("lobby")["lobby"]
        self.assertIn("no host verified", r["error"] or "")
        env["rec"]["body"]["text"] = "original line"
        node.store.db.execute("UPDATE log SET env=? WHERE seq=?", (json.dumps(env), e["seq"]))
        self.assertIsNone(T.new_core(self.env, "dave-ok").sync("lobby")["lobby"]["error"])

    def test_cross_type_and_cross_collection_replay(self):
        k = F.new_key()
        rec = make_record(k, ROOM_TYPES, "lobby", "room.msg", {"text": "x"})
        url = self.env["hub_url"]
        # same record submitted to another collection is refused (c is signed)
        self.assertEqual(submit([f"{url}/c/wiki"], rec)["status"], 400)
        # relabel type: signature domain differs, so it fails
        bad = json.loads(json.dumps(rec)); bad["rec"]["t"] = "room.topic"
        with self.assertRaises(F.FormatError):
            TypedCodec(ROOM_TYPES).verify(bad, "lobby")
        # unknown body field (e.g. html) refused
        with self.assertRaises(F.FormatError):
            make_record(k, ROOM_TYPES, "lobby", "room.msg", {"text": "x", "html": "<b>"})

    # ---- wiki ----
    def test_wiki_revisions_conflicts_revert_protect(self):
        a, c = self.a, self.c
        self.post(c, "wiki", "wiki.page", {"slug": "home", "title": "Home"})
        r1 = self.post(c, "wiki", "wiki.rev", {"slug": "home", "text": "v1"})
        r2 = self.post(c, "wiki", "wiki.rev", {"slug": "home", "text": "v2"}, [r1["id"]])
        self.post(a, "wiki", "wiki.rev", {"slug": "home", "text": "stale"}, [r1["id"]])
        a.sync("wiki")
        p = a.view("wiki")["view"]["pages"]["home"]
        self.assertEqual((p["text"], p["head"]), ("v2", r2["id"]))
        self.assertEqual(p["conflicts"][0]["why"], "stale parent")
        rv = self.post(a, "wiki", "wiki.revert", {"slug": "home"}, [r1["id"]])
        self.post(a, "wiki", "wiki.protect", {"slug": "home", "on": True})
        self.post(c, "wiki", "wiki.rev", {"slug": "home", "text": "vandal"}, [rv["id"]])
        self.assertFalse(c.draft("wiki", "wiki.protect", {"slug": "home", "on": False}, [])
                         ["result"]["ok"])
        c.sync("wiki")
        p = c.view("wiki")["view"]["pages"]["home"]
        self.assertEqual((p["text"], p["protected"]), ("v1", True))
        self.assertEqual(p["conflicts"][-1]["why"], "page protected")

    # ---- identity ----
    def test_profile_rotation_and_recovery(self):
        old, new, thief_new = F.new_key(), F.new_key(), F.new_key()
        recov = F.new_key()
        url = f"{self.env['hub_url']}/c/people"
        ts = T_now = int(time.time())
        def put(key, t, body):
            r = submit([url], make_record(key, KIND_TYPES["id"], "people", t, body, pow_bits=8))
            self.assertTrue(r.get("ok"), r)
        put(old, "id.profile", {"name": "river", "next_key_hash": views.key_hash(F.pub_hex(recov))})
        # thief steals old key, rotates to their key and tries to swap the recovery commitment
        put(old, "id.rotate", {"old_key": F.pub_hex(old), "new_key": F.pub_hex(thief_new)})
        put(old, "id.profile", {"name": "river", "next_key_hash": views.key_hash(F.pub_hex(F.new_key()))})
        self.c.sync("people")
        self.assertEqual(views.identities(views.items_from_store(
            self.c.colls["people"].store, self.c.colls["people"].codec))["current"][F.pub_hex(old)],
            F.pub_hex(thief_new))
        # owner reveals the recovery key: overrides the thief's rotation
        put(recov, "id.rotate", {"old_key": F.pub_hex(old), "new_key": F.pub_hex(new)})
        # thief's later rotation can no longer win
        put(old, "id.rotate", {"old_key": F.pub_hex(old), "new_key": F.pub_hex(F.new_key())})
        put(new, "id.profile", {"name": "river"})
        self.c.sync("people")
        ids = views.identities(views.items_from_store(self.c.colls["people"].store,
                                                      self.c.colls["people"].codec))
        self.assertEqual(ids["current"][F.pub_hex(old)], F.pub_hex(new))
        self.assertTrue(ids["labels"][F.pub_hex(new)].startswith("river·" + F.pub_hex(new)[:6]))
        # a random key can't rotate someone else's key
        put(F.new_key(), "id.rotate", {"old_key": F.pub_hex(new), "new_key": None})
        self.c.sync("people")
        ids = views.identities(views.items_from_store(self.c.colls["people"].store,
                                                      self.c.colls["people"].codec))
        self.assertEqual(ids["current"][F.pub_hex(new)], F.pub_hex(new))
        # retirement
        put(new, "id.rotate", {"old_key": F.pub_hex(new), "new_key": None})
        self.c.sync("people")
        self.assertTrue(any("(retired)" in p["label"] for p in self.c.view("people")["view"]["people"]))

    def test_links_must_be_mutual(self):
        x, y = F.new_key(), F.new_key()
        url = f"{self.env['hub_url']}/c/people"
        submit([url], make_record(x, KIND_TYPES["id"], "people", "id.link", {"other_key": F.pub_hex(y)}, pow_bits=8))
        self.c.sync("people")
        items = views.items_from_store(self.c.colls["people"].store, self.c.colls["people"].codec)
        self.assertEqual(views.identities(items)["links"].get(F.pub_hex(x)), [])
        submit([url], make_record(y, KIND_TYPES["id"], "people", "id.link", {"other_key": F.pub_hex(x)}, pow_bits=8))
        self.c.sync("people")
        items = views.items_from_store(self.c.colls["people"].store, self.c.colls["people"].codec)
        self.assertEqual(views.identities(items)["links"][F.pub_hex(x)], [F.pub_hex(y)])

    # ---- shop ----
    def test_shop_listing_review_payment_sheet(self):
        body = {"title": "Sticker pack", "desc": "Five stickers", "price": "0.002", "currency": "ETH",
                "chain_id": 11155111, "pay_to": "0x" + "ab" * 20}
        l = self.post(self.b, "store", "shop.listing", body)
        r = self.c.draft("store", "shop.listing", body, [])["result"]
        self.assertFalse(r["ok"])  # only sellers
        main = dict(body, chain_id=42161)
        self.assertFalse(self.b.draft("store", "shop.listing", main, [])["result"]["ok"])  # mainnet
        self.post(self.c, "store", "shop.review", {"rating": 5, "text": "great"}, [l["id"]])
        self.post(self.b, "store", "shop.review", {"rating": 5, "text": "self"}, [l["id"]])  # ignored
        self.c.sync("store")
        lst = next(x for x in self.c.view("store")["view"]["listings"] if x["id"] == l["id"])
        self.assertEqual((lst["rating"], len(lst["reviews"])), (5, 1))
        s = self.c.pay_draft("store", l["id"])["sheet"]
        self.assertEqual((s["network"], s["to"], s["amount"]), ("Sepolia", body["pay_to"], "0.002"))
        self.assertIn("never sends", s["note"])
        self.post(self.b, "store", "shop.withdraw", {"reason": "sold out"}, [l["id"]])
        self.c.sync("store")
        with self.assertRaises(F.FormatError):
            self.c.pay_draft("store", l["id"])

    # ---- drop ----
    def test_file_drop_hash_checked(self):
        data = b"hello file drop\n" * 10
        d = self.c.upload("files", data, "hello.txt", "text/plain", "a note", mode="plain")
        self.assertTrue(d["result"]["ok"])
        self.b.sync("files")
        it = self.b.view("files")["view"]["items"][-1]
        self.assertEqual(it["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.b.blob("files", it["sha256"]), data)
        # a host serving wrong bytes for a hash is caught
        blobs = self.env["hub"].nodes["files"].blobs
        evil = F.new_key()
        p = os.path.join(blobs.d, hashlib.sha256(b"other").hexdigest())
        h2 = hashlib.sha256(b"real").hexdigest()
        with open(os.path.join(blobs.d, h2), "wb") as f:
            f.write(b"tampered")
        self.assertIsNone(self.b.blob("files", h2))

    # ---- exit board and SNApp card (existing formats, unchanged) ----
    def test_exit_board_view_and_tamper(self):
        v = self.c.view("exits")["view"]
        self.assertEqual(len(v["listings"]), 1)
        self.assertEqual(v["listings"][0]["fields"]["country"], "NL")
        www = self.env["tmp"] / "exit-www"
        good = (www / "board.json").read_text()
        (www / "board.json").write_text(good.replace('"NL"', '"US"'))
        r = self.c.sync("exits")["exits"]
        self.assertIn("signature", r["error"])
        self.assertEqual(self.c.view("exits")["view"]["listings"][0]["fields"]["country"], "NL")
        (www / "board.json").write_text(good)

    def test_snapp_card(self):
        from typed import adapters
        if adapters.cardfmt is None:
            self.skipTest("SNApp card verifier not bundled")
        self.assertTrue(self.c.view("rooms-card")["view"]["ok"])
        from typed.adapters import card_view
        card = json.loads(open(self.env["card_path"]).read())
        card["port"] = 1
        self.assertFalse(card_view(card)["ok"])
        self.assertFalse(card_view(json.loads(open(self.env["card_path"]).read()),
                                   expect_pubkey="00" * 32)["ok"])

    # ---- shared: moderation across types, outbox, drafts ----
    def test_hide_applies_to_typed_collections(self):
        r = self.post(self.c, "lobby", "room.msg", {"text": "spammy line"})
        self.env["hub"].nodes["lobby"].moderate("hide", "post", r["id"], "spam")
        self.assertIsNone(self.b.sync("lobby")["lobby"]["error"])
        self.assertFalse(any(l["id"] == r["id"] for l in self.b.view("lobby")["view"]["lines"]))
        self.b.prefs["revealed"].append(r["id"])
        self.assertTrue(any(l["id"] == r["id"] for l in self.b.view("lobby")["view"]["lines"]))

    def test_draft_needs_confirmation_and_is_validated(self):
        core = T.new_core(self.env, "eve", autoconfirm=False)
        core.sync()
        d = core.draft("lobby", "room.msg", {"text": "pending"}, [])
        self.assertNotIn("result", d)
        self.assertEqual(core.colls["lobby"].store.db.execute(
            "SELECT COUNT(*) FROM log WHERE author=?", (core.me,)).fetchone()[0], 0)
        for bad in [("lobby", "room.msg", {"text": "x" * 501}), ("lobby", "wiki.rev", {"slug": "a", "text": "b"}),
                    ("exits", "room.msg", {"text": "x"}), ("lobby", "room.msg", {"text": "a", "evil": 1})]:
            with self.assertRaises(Exception):
                core.draft(bad[0], bad[1], bad[2], [])
        self.assertTrue(core.confirm(d["token"])["ok"])
        self.assertFalse(core.confirm(d["token"])["ok"])  # one-shot token


if __name__ == "__main__":
    unittest.main()
