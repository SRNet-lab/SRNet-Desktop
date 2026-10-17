"""Shipped default config: placeholders -> empty sidebar; real pins -> lobby + directory pre-joined;
user config layered on top; leaving a pre-joined collection stays left; directory Join for
file drops, stores and mail hosts; `srnet snapps` without --config."""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mbfmt as F  # noqa: E402
from typed import clientconfig as CC  # noqa: E402
from typed import joins as J  # noqa: E402
from typed import mailhost as MH  # noqa: E402
from typed.core import Core  # noqa: E402
from typed.hub import Hub  # noqa: E402
from typed.node import serve, url_of  # noqa: E402
from typed.record import KIND_TYPES, make_record  # noqa: E402
from typed.sync import submit  # noqa: E402

LOW = {k: {"pow_bits": 4, "new_key_pow_bits": 6} for k in ("room", "directory", "id", "drop", "shop")}


# an unconfigured build: the shipped layout with every value still a placeholder
PLACEHOLDERS = {"collections": [
    {"id": "lobby", "label": "Lobby", "kind": "room", "name": "lobby",
     "hosts": ["http://REPLACE-ME-LOBBY-HOST.sesh/c/lobby"], "root_pin": "REPLACE-ME-LOBBY-ROOT-KEY-64-HEX"},
    {"id": "directory", "label": "Directory", "kind": "directory", "name": "directory",
     "hosts": ["http://REPLACE-ME-DIRECTORY-HOST.sesh/c/directory"], "root_pin": "REPLACE-ME-DIRECTORY-ROOT-KEY-64-HEX"}],
    "mail": {"hosts": ["http://REPLACE-ME-MAIL-HOST.sesh"]}}


def placeholder_file():
    p = Path(tempfile.mkdtemp(prefix="dflt-ph-")) / "defaults.json"
    p.write_text(json.dumps(PLACEHOLDERS))
    return p


SESH = re.compile(r"^http://[ybndrfg8ejkmcpqxot1uwisza345h769]{52}\.sesh(/c/[a-z]+)?$")


class Shipped(unittest.TestCase):
    def test_shipped_file_has_real_pinned_slots(self):
        raw = json.loads(CC.DEFAULTS.read_text())
        ids = {c["id"]: c for c in raw["collections"]}
        self.assertEqual(set(ids), {"lobby", "directory"})
        self.assertEqual(ids["lobby"]["kind"], "room")
        self.assertEqual(ids["directory"]["kind"], "directory")
        for c in ids.values():
            self.assertRegex(c["root_pin"], CC.HEX64)
            self.assertRegex(c["hosts"][0], SESH)
            self.assertTrue(c["hosts"][0].endswith("/c/" + c["name"]))
        self.assertRegex(raw["mail"]["hosts"][0], SESH)
        cfg = CC.load()
        self.assertEqual([c["id"] for c in cfg["collections"]], ["lobby", "directory"])
        self.assertNotIn("_about", cfg)

    def test_placeholders_give_empty_config(self):
        cfg = CC.load(None, placeholder_file())
        self.assertEqual(cfg["collections"], [])
        self.assertEqual(cfg["mail"]["hosts"], [])

    def test_unconfigured_core_starts_empty(self):
        c = Core(tempfile.mkdtemp(prefix="dflt-empty-"), CC.load(None, placeholder_file()))
        self.assertEqual(c.sidebar(), {"groups": []})
        self.assertEqual(c.mail.summary()["hosts"], [])
        c.sync()  # nothing to sync, no errors

    def test_bad_pin_is_skipped(self):
        cfg = CC.merge({"collections": [{"id": "x", "kind": "room", "name": "x", "hosts": ["http://a"],
                                         "root_pin": "nothex"}]}, None)
        self.assertEqual(cfg["collections"], [])

    def test_layering(self):
        d = {"collections": [{"id": "lobby", "kind": "room", "name": "lobby", "root_pin": "aa" * 32, "hosts": ["http://l"]},
                             {"id": "directory", "kind": "directory", "name": "directory", "root_pin": "bb" * 32, "hosts": ["http://d"]}],
             "mail": {"hosts": ["http://m1"], "attach_drop": "files"}, "html_sites": {"a.sesh": "x"}}
        u = {"collections": [{"id": "lobby", "disabled": True},
                             {"id": "directory", "kind": "directory", "name": "directory", "root_pin": "cc" * 32, "hosts": ["http://d2"]},
                             {"id": "mine", "kind": "room", "name": "mine", "root_pin": "dd" * 32, "hosts": ["http://x"]}],
             "mail": {"hosts": ["http://m2"]}, "html_sites": {}}
        m = CC.merge(d, u)
        self.assertEqual([c["id"] for c in m["collections"]], ["directory", "mine"])
        self.assertEqual(m["collections"][0]["root_pin"], "cc" * 32)
        self.assertEqual(m["mail"], {"hosts": ["http://m2"], "attach_drop": "files"})
        self.assertEqual(m["html_sites"], {})
        self.assertEqual(CC.merge(d, None)["mail"]["hosts"], ["http://m1"])


class Fixture:
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="srnet-dflt-"))
        cls.root = F.key_from_seed(os.urandom(32))
        cls.pin = F.pub_hex(cls.root)
        hk = F.key_from_seed(os.urandom(32))
        cls.hub = Hub(str(cls.tmp / "hub"), cls.root, hk,
                      [("room", "lobby"), ("directory", "directory"), ("drop", "files"), ("shop", "store")],
                      {}, pol_override=LOW)
        cls.srv = serve(cls.hub)
        cls.url = url_of(cls.srv)
        cls.host = urlparse(cls.url).netloc
        # a mail host linked to the same root (what the host pack's `link` writes)
        mh = MH.Host(str(cls.tmp / "mail"))
        mh.delegation = F.make_delegation(cls.root, "mail", mh.id, int(__import__("time").time()) + 86400)
        cls.msrv = MH.serve(mh, "127.0.0.1", 0)
        cls.mail_url = f"http://127.0.0.1:{cls.msrv.server_address[1]}"
        cls.mail_host = urlparse(cls.mail_url).netloc
        # an impostor mail host: delegation signed by some other key
        bad = MH.Host(str(cls.tmp / "mail-bad"))
        bad.delegation = F.make_delegation(F.key_from_seed(os.urandom(32)), "mail", bad.id,
                                           int(__import__("time").time()) + 86400)
        cls.bsrv = MH.serve(bad, "127.0.0.1", 0)
        cls.bad_host = f"127.0.0.1:{cls.bsrv.server_address[1]}"
        cls.defaults = cls.tmp / "defaults.json"
        cls.defaults.write_text(json.dumps({
            "collections": [
                {"id": "lobby", "label": "Lobby", "kind": "room", "name": "lobby",
                 "hosts": [f"{cls.url}/c/lobby"], "root_pin": cls.pin},
                {"id": "directory", "label": "Directory", "kind": "directory", "name": "directory",
                 "hosts": [f"{cls.url}/c/directory"], "root_pin": cls.pin}],
            "mail": {"hosts": [cls.mail_url]}}))
        links = {"files": J.make(cls.host, "drop", "files", cls.pin),
                 "store": J.make(cls.host, "shop", "store", cls.pin),
                 "mail": J.make(cls.mail_host, "mail", "mail", cls.pin)}
        cls.links = links
        for title, ln in (("Demo file drop", links["files"]), ("Demo store", links["store"]),
                          ("Free mail", links["mail"])):
            env = make_record(cls.root, KIND_TYPES["directory"], "directory", "directory.entry",
                              {"link": ln, "title": title, "desc": None}, [], pow_bits=6)
            assert submit([f"{cls.url}/c/directory"], env).get("ok")

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv, cls.msrv, cls.bsrv):
            s.shutdown()

    def core(self, name, user=None):
        d = self.tmp / "c" / name
        d.mkdir(parents=True, exist_ok=True)
        up = None
        if user is not None:
            up = d.parent / f"{name}.user.json"
            up.write_text(json.dumps(user))
        return Core(str(d), CC.load(up, self.defaults), autoconfirm=True)


class PreJoined(Fixture, unittest.TestCase):

    def test_fresh_install_has_lobby_and_directory_pinned(self):
        c = self.core("fresh")
        items = [i for g in c.sidebar()["groups"] for i in g["items"]]
        self.assertEqual(sorted(i["id"] for i in items), ["directory", "lobby"])
        self.assertEqual({i["label"] for i in items}, {"Lobby", "Directory"})
        for cid in ("lobby", "directory"):
            self.assertEqual(c.prefs["joined"][cid]["cfg"]["root_pin"], self.pin)
            self.assertEqual(c.prefs["joined"][cid]["source"], "config")
        st = c.sync()
        self.assertFalse(st["lobby"].get("root_mismatch"))
        self.assertEqual(c.mail.summary()["hosts"], [self.mail_url])

    def test_wrong_shipped_pin_blocks(self):
        bad = json.loads(self.defaults.read_text())
        bad["collections"][0]["root_pin"] = "ab" * 32
        p = self.tmp / "bad-defaults.json"
        p.write_text(json.dumps(bad))
        d = self.tmp / "c" / "badpin"
        d.mkdir(parents=True, exist_ok=True)
        c = Core(str(d), CC.load(None, p))
        self.assertEqual(c.sync("lobby")["lobby"]["root_mismatch"]["offered"], self.pin)

    def test_user_overlay_disables_and_adds(self):
        c = self.core("overlay", {"collections": [{"id": "lobby", "disabled": True}]})
        self.assertEqual(list(c.colls), ["directory"])

    def test_leave_prejoined_stays_left(self):
        c = self.core("leave")
        c.leave("lobby")
        c2 = self.core("leave")  # restart with the same shipped defaults
        self.assertNotIn("lobby", c2.colls)
        self.assertIn("directory", c2.colls)
        r = c2.join(J.make(self.host, "room", "lobby", self.pin))  # joining again by link brings it back
        self.assertTrue(r["joined"])
        c3 = self.core("leave")
        self.assertIn(r["id"], c3.colls)

    def test_directory_join_buttons_cover_drop_store_mail(self):
        c = self.core("dirjoin", {"mail": {"hosts": []}})
        c.sync("directory")
        es = c.view("directory")["view"]["entries"]
        self.assertEqual(sorted(e["kind"] for e in es), ["drop", "mail", "shop"])
        by = {e["kind"]: e for e in es}
        self.assertTrue(c.join(by["drop"]["link"])["joined"])
        self.assertTrue(c.join(by["shop"]["link"])["joined"])
        self.assertEqual({c.colls[i].kind for i in c.colls}, {"room", "directory", "drop", "shop"})
        pv = c.join_preview(by["mail"]["link"])
        self.assertEqual((pv["kind"], pv["root"], pv["already_joined"]), ("mail", self.pin, []))
        r = c.join(by["mail"]["link"])
        self.assertTrue(r["joined"] and r["mail"])
        self.assertEqual(c.mail.st["hosts"], [self.mail_url])
        self.assertNotIn("error", r["register"])
        self.assertEqual(c.join_preview(by["mail"]["link"])["already_joined"], ["mail"])
        self.assertNotIn("mail", c.colls)  # a mail host is not a sidebar collection

    def test_mail_host_join_verifies_root(self):
        c = self.core("mailbad", {"mail": {"hosts": []}})
        with self.assertRaises(J.LinkError):  # link pins our root, host's delegation is someone else's
            c.join(J.make(self.bad_host, "mail", "mail", self.pin))
        with self.assertRaises(J.LinkError):  # a host with no mail role at all
            c.join_preview(J.make(self.host, "mail", "mail", self.pin))
        self.assertEqual(c.mail.st["hosts"], [])


try:
    from playwright.sync_api import sync_playwright
    from test_room_ui import CHROME
except ImportError:
    sync_playwright = CHROME = None


@unittest.skipUnless(sync_playwright and CHROME, "playwright or Chrome not installed")
class DefaultsUI(Fixture, unittest.TestCase):
    """Real Chrome: empty sidebar text; pre-joined sidebar; directory Join buttons per kind."""
    def browse(self, core, path):
        from typed.core import serve as core_serve
        srv = core_serve(core, 0, sync_every=0.5)
        self.addCleanup(srv.shutdown)
        p = sync_playwright().start()
        self.addCleanup(p.stop)
        b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        self.addCleanup(b.close)
        pg = b.new_page()
        pg.goto(f"http://127.0.0.1:{srv.server_address[1]}{path}")
        return pg

    def test_empty_sidebar_is_friendly(self):
        d = self.tmp / "c" / "ui-empty"
        d.mkdir(parents=True, exist_ok=True)
        pg = self.browse(Core(str(d), CC.load(None, placeholder_file())), "/")
        pg.wait_for_selector("#side-empty", timeout=20000)
        self.assertIn("Nothing joined yet", pg.inner_text("#side-empty"))

    def test_prejoined_sidebar_and_directory_buttons(self):
        c = self.core("ui-dir", {"mail": {"hosts": []}})
        c.sync()
        pg = self.browse(c, "/?c=directory")
        pg.wait_for_selector("#side .item[data-id=lobby]", timeout=20000)
        self.assertEqual(pg.locator("#side-empty").count(), 0)
        side = pg.inner_text("#groups")
        self.assertIn("Lobby", side)
        self.assertIn("Directory", side)
        fr = pg.frame_locator("#tpl")
        fr.locator("button", has_text="Add mail host").wait_for(timeout=20000)
        for label in ("Join file drop", "Join store"):
            self.assertEqual(fr.locator("button", has_text=label).count(), 1, label)
        fr.locator("button", has_text="Join file drop").click()
        pg.wait_for_selector("#sheet:not([hidden])", timeout=10000)
        self.assertIn("type: drop", pg.inner_text("#sheet"))
        pg.click("#sheet-buttons button:has-text('Join')")
        pg.wait_for_selector("#side .item[data-id=files]", timeout=20000)
        pg.goto(pg.url.split("?")[0] + "?c=directory")
        fr = pg.frame_locator("#tpl")
        fr.locator("button", has_text="Add mail host").click()
        pg.wait_for_selector("#sheet:not([hidden])", timeout=10000)
        self.assertIn("as a mail host?", pg.inner_text("#sheet"))
        pg.click("#sheet-buttons button:has-text('Add mail host')")
        pg.wait_for_url("**/mail", timeout=20000)
        self.assertEqual(c.mail.st["hosts"], [self.mail_url])


class CliSnapps(unittest.TestCase):
    def run_cli(self, home, *extra):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client-cli"))
        from srnet import cli
        seen = {}
        orig = cli._run_child
        cli._run_child = lambda cmd, cwd=None: seen.setdefault("cmd", cmd) and 0
        try:
            rc = cli.main(["--home", str(home), "snapps", "--port", "48999", *extra])
        finally:
            cli._run_child = orig
        return rc, seen.get("cmd")

    def test_config_is_optional(self):
        home = Path(tempfile.mkdtemp(prefix="srnet-home-"))
        rc, cmd = self.run_cli(home)
        self.assertEqual(rc, 0)
        self.assertNotIn("--config", cmd)
        (home / "client.json").write_text("{}")
        rc, cmd = self.run_cli(home)
        self.assertEqual(cmd[cmd.index("--config") + 1], str((home / "client.json").resolve()))
        rc, cmd = self.run_cli(home, "--config", str(home / "missing.json"))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
