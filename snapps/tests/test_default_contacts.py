"""Shipped first contacts (mail.contacts): SRNet is a contact out of the box, its mail lands in
Inbox, the user can message it, and user config / user removal win."""
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from typed import clientconfig as CC, mail as M, mailhost as MH  # noqa: E402
from typed.mailclient import MailClient  # noqa: E402
from helpers import tmpdir  # noqa: E402


class Shipped(unittest.TestCase):
    def test_srnet_is_a_valid_signed_card(self):
        cs = CC.load()["mail"]["contacts"]
        self.assertEqual([c["name"] for c in cs], ["SRNet"])
        env, name = M.parse_link(cs[0]["link"])
        card = M.verify_card(env)
        self.assertEqual(name, "SRNet")
        self.assertEqual(card["hosts"], CC.load()["mail"]["hosts"])

    def test_user_contacts_list_replaces_and_placeholders_drop(self):
        d = {"mail": {"hosts": ["http://a.sesh"], "contacts": [{"name": "X", "link": "srnet://mail/REPLACE-ME"}]}}
        self.assertEqual(CC.merge(d, None)["mail"]["contacts"], [])
        self.assertEqual(CC.merge(CC.load(), {"mail": {"contacts": []}})["mail"]["contacts"], [])
        self.assertEqual(CC.merge(CC.load(), {"mail": {"hosts": ["http://b.sesh"]}})["mail"]["contacts"],
                         CC.load()["mail"]["contacts"])


class Live(unittest.TestCase):
    def setUp(self):
        self.t = tmpdir()
        self.srv = MH.serve(MH.Host(os.path.join(self.t, "h")), purge=False)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.team = MailClient(os.path.join(self.t, "team"), os.urandom(32), [self.url])
        self.team.register()
        self.contacts = [{"name": "SRNet", "link": self.team.link("SRNet")}]

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
        shutil.rmtree(self.t, ignore_errors=True)

    def user(self, name="u"):
        u = MailClient(os.path.join(self.t, name), os.urandom(32), [self.url], contacts=self.contacts)
        u.register()
        return u

    def test_new_user_gets_srnet_in_inbox_both_ways(self):
        u = self.user()
        self.assertIn(self.team.me, u.st["contacts"])
        self.assertEqual(u.name_of(self.team.me), "SRNet")
        # SRNet -> user lands in Inbox, not Requests
        self.assertTrue(self.team.send(u.link("U"), "Welcome", "hi")["ok"])
        u.fetch()
        self.assertEqual([m["subject"] for m in u.folder("inbox")], ["Welcome"])
        self.assertEqual(u.folder("requests"), [])
        # user -> SRNet works by its name
        self.assertTrue(u.send("SRNet", "Hello", "test")["ok"])
        self.team.fetch()
        self.assertEqual([m["subject"] for m in self.team.folder("requests")], ["Hello"])

    def test_removed_or_renamed_stays_that_way(self):
        u = self.user()
        u.petname(f"srnet://mail/{self.team.me}", "Team")
        u.remove_contact(self.team.me)
        seed = os.urandom(32)
        r = MailClient(os.path.join(self.t, "r"), seed, [self.url], contacts=self.contacts)
        r.remove_contact(self.team.me)
        r2 = MailClient(os.path.join(self.t, "r"), seed, [self.url], contacts=self.contacts)  # restart
        self.assertNotIn(self.team.me, r2.st["contacts"])
        u.seed_contacts(self.contacts)
        self.assertNotIn(self.team.me, u.st["contacts"])
        self.assertEqual(u.name_of(self.team.me), "Team")

    def test_bad_card_is_skipped(self):
        u = MailClient(os.path.join(self.t, "b"), os.urandom(32), [self.url],
                       contacts=[{"name": "bad", "link": "srnet://mail/nope"}])
        self.assertEqual(u.st["contacts"], {})


if __name__ == "__main__":
    unittest.main()
