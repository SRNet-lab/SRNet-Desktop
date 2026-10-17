"""Join / return flow: link parsing, signed probe, persisted sidebar, TOFU, unread, share, directory."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mbfmt as F  # noqa: E402
from typed import joins as J  # noqa: E402
from typed import testbed as T  # noqa: E402
from typed.core import Core  # noqa: E402
from typed.hub import Hub  # noqa: E402
from typed.node import serve, url_of  # noqa: E402
from typed.record import KIND_TYPES, make_record  # noqa: E402
from typed.sync import submit  # noqa: E402

SESH = "y" * 52 + ".sesh"
ROOT = "ab" * 32
LOW = {k: {"pow_bits": 4, "new_key_pow_bits": 6} for k in ("room", "directory", "id")}


class Links(unittest.TestCase):
    def test_good(self):
        l = J.parse(f"srnet://{SESH}/room/lobby#root={ROOT}")
        self.assertEqual(l, {"host": SESH, "kind": "room", "name": "lobby", "root": ROOT})
        self.assertEqual(J.parse(SESH), {"host": SESH, "kind": None, "name": None, "root": None})
        self.assertEqual(J.parse(f"  srnet://{SESH}/  ")["host"], SESH)
        self.assertEqual(J.parse("srnet://127.0.0.1:8080/wiki/w")["kind"], "wiki")
        self.assertEqual(J.make(SESH, "room", "lobby", ROOT), f"srnet://{SESH}/room/lobby#root={ROOT}")

    def test_rejects_injection(self):
        bad = [
            "", "x" * 500, "http://evil.example/room/lobby", "file:///etc/passwd", "javascript:alert(1)",
            f"srnet://evil.example/room/lobby", f"srnet://{SESH}@evil.example/room/lobby",
            f"srnet://user@{SESH}/room/lobby", f"srnet://{SESH}/room/lobby?x=1",
            f"srnet://{SESH}/room/../../etc", f"srnet://{SESH}/room/lobby/extra",
            f"srnet://{SESH}/nope/lobby", f"srnet://{SESH}/room/Lobby", f"srnet://{SESH}/room/lo%2fby",
            f"srnet://{SESH}/room/lobby#root=zz", f"srnet://{SESH}/room/lobby#root={ROOT}&x",
            f"srnet://{SESH}/room/lob by", f"srnet://{SESH}/room/lobby\n", f"srnet://{SESH}/room/lo\x00by",
            f"srnet://{SESH}/room/<script>", f"srnet://{SESH}/room/lоbby",  # Cyrillic o
            "srnet://127.0.0.1/room/x", "srnet://127.0.0.1:99999/room/x", "srnet://10.0.0.1:80/room/x",
            f"srnet://{SESH}\\@evil/room/x", "srnet://" + "y" * 51 + ".sesh/room/x",
            f"srnet://{SESH}/room/" + "a" * 40, {"host": SESH}, None]
        for b in bad:
            with self.assertRaises(J.LinkError, msg=repr(b)):
                J.parse(b)
        with self.assertRaises(J.LinkError):
            J.make("evil.example", "room", "lobby", ROOT)


def link_for(url, kind, name, root):
    return J.make(urlparse(url).netloc, kind, name, root)


class JoinFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = T.start(mirror=False)
        cls.tmp = Path(tempfile.mkdtemp(prefix="srnet-join-"))
        cls.rootseed = os.urandom(32)
        cls.root = F.key_from_seed(cls.rootseed)
        cls.hk = F.key_from_seed(os.urandom(32))
        cls.hub2 = Hub(str(cls.tmp / "hub2"), cls.root, cls.hk,
                       [("room", "news"), ("room", "lobby"), ("directory", "dir")], {}, pol_override=LOW)
        cls.srv = serve(cls.hub2)
        cls.url = url_of(cls.srv)
        cls.host = urlparse(cls.url).netloc
        cls.pin = F.pub_hex(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        T.stop(cls.env)

    def core(self, name):
        d = self.tmp / "c" / name
        d.mkdir(parents=True, exist_ok=True)
        return Core(str(d), T.client_config(self.env), autoconfirm=True)

    def post(self, coll, text, seed=None):
        k = F.key_from_seed(seed or os.urandom(32))
        env = make_record(k, KIND_TYPES["room"], coll, "room.msg", {"text": text, "reply_to": None}, [],
                          pow_bits=6)
        r = submit([f"{self.url}/c/{coll}"], env)
        self.assertTrue(r.get("ok"), r)

    def test_preview_join_persist_unread(self):
        c = self.core("a")
        link = link_for(self.url, "room", "news", self.pin)
        p = c.join_preview(link)
        self.assertEqual((p["name"], p["kind"], p["root"]), ("news", "room", self.pin))
        self.assertTrue(p["first_seen"])
        self.assertNotIn("news", c.colls)  # preview saves nothing
        # a link carrying the wrong root is refused outright
        with self.assertRaises(J.LinkError):
            c.join_preview(link_for(self.url, "room", "news", "cd" * 32))
        r = c.join(link, label="News desk")
        self.assertTrue(r["joined"])
        cid = r["id"]
        self.assertEqual(cid, "news")
        # name collision with config "lobby" gets a fresh local id
        r2 = c.join(link_for(self.url, "room", "lobby", self.pin))
        self.assertEqual(r2["id"], "lobby-2")
        self.post("news", "hello one"); self.post("news", "hello two")
        c.sync(cid)
        sb = {i["id"]: i for g in c.sidebar()["groups"] for i in g["items"]}
        self.assertEqual(sb[cid]["unread"], 2)
        self.assertEqual(sb[cid]["label"], "News desk")
        self.assertIn("lobby", sb)  # config collections are pre-joined
        self.assertNotIn("exits", sb)  # Exits is a built-in, not a sidebar item
        c.draft(cid, "room.msg", {"text": "mine", "reply_to": None}, [])
        self.assertEqual(c.unread(cid), 2)  # my own posts aren't unread
        c.view(cid, read=True)
        self.assertEqual(c.unread(cid), 0)
        self.post("news", "three")
        c.sync(cid)
        self.assertEqual(c.unread(cid), 1)
        c.update_joined(cid, muted=True, pinned=True)
        self.assertEqual(c.unread(cid), 0)
        groups = c.sidebar()["groups"]
        self.assertEqual(groups[0]["host"], self.host)  # pinned group first
        # restart: everything comes back without config edits
        c2 = self.core("a")
        self.assertIn(cid, c2.colls)
        self.assertIn("lobby-2", c2.colls)
        e = c2.prefs["joined"][cid]
        self.assertEqual((e["label"], e["muted"], e["pinned"]), ("News desk", True, True))
        self.assertTrue(any(it["body"]["text"] == "hello one" for it in c2._items(c2.colls[cid])[0]))
        # share produces a link that parses back to the same collection
        sl = c2.share(cid)["link"]
        self.assertEqual(J.parse(sl), {"host": self.host, "kind": "room", "name": "news", "root": self.pin})
        self.assertEqual(J.parse(c2.share("lobby")["link"])["root"], self.env["hub_pin"])
        # join again: no duplicate
        self.assertEqual(c2.join(sl)["id"], cid)
        # leave persists, including for config collections
        c2.leave(cid); c2.leave("lobby")
        c3 = self.core("a")
        self.assertNotIn(cid, c3.colls)
        self.assertNotIn("lobby", c3.colls)
        self.assertIn("wiki", c3.colls)

    def test_bare_host_lists_choices(self):
        c = self.core("b")
        p = c.join_preview("srnet://" + self.host)
        names = sorted(x["name"] for x in p["choices"])
        self.assertEqual(names, ["dir", "lobby", "news"])
        self.assertTrue(all(J.parse(x["link"])["root"] == self.pin for x in p["choices"]))

    def test_tofu_mismatch_blocks_until_retrust(self):
        tmp = Path(tempfile.mkdtemp(prefix="srnet-rot-"))
        r1 = F.key_from_seed(os.urandom(32))
        hub = Hub(str(tmp / "h1"), r1, self.hk, [("room", "ops")], {}, pol_override=LOW)
        srv = serve(hub)
        try:
            url = url_of(srv)
            c = self.core("rot")
            c.join(f"srnet://{urlparse(url).netloc}/room/ops")  # no #root: trust on first use
            self.assertEqual(c.prefs["trust"][f"{urlparse(url).netloc}/room/ops"], F.pub_hex(r1))
            # host's root key changes (takeover or rotation)
            r2 = F.key_from_seed(os.urandom(32))
            hub.nodes["ops"] = Hub(str(tmp / "h2"), r2, self.hk, [("room", "ops")], {}, pol_override=LOW).nodes["ops"]
            st = c.sync("ops")["ops"]
            self.assertEqual(st["root_mismatch"], {"pinned": F.pub_hex(r1), "offered": F.pub_hex(r2)})
            sb = {i["id"]: i for g in c.sidebar()["groups"] for i in g["items"]}
            self.assertTrue(sb["ops"]["root_mismatch"])
            # re-joining via a no-root link is also blocked
            p = c.join(f"srnet://{urlparse(url).netloc}/room/ops")
            self.assertTrue(p.get("root_mismatch") or p["id"] == "ops")
            self.assertTrue(c.join_preview(f"srnet://{urlparse(url).netloc}/room/ops")["root_mismatch"])
            # re-trust needs the explicit phrase
            with self.assertRaises(ValueError):
                c.retrust("ops", F.pub_hex(r2), "yes")
            with self.assertRaises(ValueError):
                c.retrust("ops", "ee" * 32, c.RETRUST_PHRASE)
            c.retrust("ops", F.pub_hex(r2), c.RETRUST_PHRASE)
            self.assertIsNone(c.colls["ops"].last.get("root_mismatch"))
            self.assertIsNone(c.colls["ops"].last.get("error"))
            self.assertEqual(c.prefs["trust"][f"{urlparse(url).netloc}/room/ops"], F.pub_hex(r2))
            c2 = self.core("rot")
            self.assertEqual(c2.colls["ops"].root_pin, F.pub_hex(r2))
        finally:
            srv.shutdown()

    def test_config_mismatch_blocks(self):
        c = self.core("cfgmm")
        c.colls["lobby"].root_pin = "ab" * 32  # as if the hub's root changed after config pinned it
        st = c.sync("lobby")["lobby"]
        self.assertEqual(st["root_mismatch"]["offered"], self.env["hub_pin"])

    def test_directory_only_lists_root_signed_entries(self):
        c = self.core("dir")
        c.join(link_for(self.url, "directory", "dir", self.pin))
        good = link_for(self.url, "room", "news", self.pin)
        for key, ln, title in ((self.root, good, "Newsroom"),
                               (F.key_from_seed(os.urandom(32)), good, "Impostor"),
                               (F.key_from_seed(os.urandom(32)), link_for(self.url, "room", "lobby", "cd" * 32), "Fake")):
            env = make_record(key, KIND_TYPES["directory"], "dir", "directory.entry",
                              {"link": ln, "title": title, "desc": None}, [], pow_bits=6)
            self.assertTrue(submit([f"{self.url}/c/dir"], env).get("ok"))
        with self.assertRaises(Exception):
            make_record(self.root, KIND_TYPES["directory"], "dir", "directory.entry",
                        {"link": "javascript:alert(1)", "title": "x", "desc": None}, [])
        c.sync("dir")
        v = c.view("dir")
        self.assertEqual(v["template"], "directory")
        self.assertEqual([e["title"] for e in v["view"]["entries"]], ["Newsroom"])
        self.assertEqual(c.join_preview(v["view"]["entries"][0]["link"])["name"], "news")


if __name__ == "__main__":
    unittest.main()
