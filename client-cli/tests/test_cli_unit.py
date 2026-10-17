import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# Never wait on a terminal: an unmocked prompt sees EOF and fails fast.
import io as _io
sys.stdin = _io.StringIO()
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

from srnet import listings, warning, killswitch, chain, state
from srnet.backend import MockBackend
from lease_flow import connect
import listing as L
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization


def _listing_doc(price="1.00", dns="10.195.0.53"):
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    d = Path(tempfile.mkdtemp())
    key = d / "exit.key"
    key.write_text((seed + pub).hex())
    doc = L.make(L.load_identity(key), price_per_hour_usdc=price, max_hours=168, payout="0x" + "aa" * 20,
                 vendor_id="0x" + "bb" * 32, chainid=31337, contract="0x" + "c0" * 20, region="test",
                 policy=["tcp/443", "tcp/80"], **({"dns": dns} if dns else {}))
    return doc, L.sesh_of(pub), d


class BuyNoExitDns(unittest.TestCase):
    def test_buy_refuses_exit_without_resolver_before_paying(self):
        from srnet.cli import main
        doc, seller, d = _listing_doc(dns=None)
        board = d / "board.json"; board.write_text(json.dumps(doc))
        home = d / "home"; home.mkdir(); (home / "visibility.ack").write_text("I UNDERSTAND\n")
        err = _io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = main(["--home", str(home), "buy", seller, "--listings", str(board),
                       "--client-sesh", "clientfixture00000000000000000000000000000000003.sesh", "--mock"])
        self.assertEqual(rc, 1)
        self.assertIn("no DNS resolver", err.getvalue())
        self.assertEqual(list(home.glob("spool/hold-*.json")), [])
        rc = main(["--home", str(home), "buy", seller, "--listings", str(board), "--system-dns",
                   "--client-sesh", "clientfixture00000000000000000000000000000000003.sesh", "--mock"])
        self.assertEqual(rc, 0)


class ListingsTest(unittest.TestCase):
    def test_drops_bad_keeps_good(self):
        good, seller, d = _listing_doc()
        bad = json.loads(json.dumps(good))
        bad["listing"]["price_per_hour_usdc"] = "0.01"
        path = d / "board.json"
        path.write_text(json.dumps({"listings": [good, bad]}))
        board = listings.load_board(path)
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0]["listing"]["seller"], seller)


class WarningTest(unittest.TestCase):
    def test_ack_required_then_cached(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch("builtins.input", return_value="nope"):
            with self.assertRaises(SystemExit):
                warning.ensure_ack(d)
        with mock.patch("builtins.input", return_value="I UNDERSTAND"):
            warning.ensure_ack(d)
        warning.ensure_ack(d)  # no input needed


class ExitDnsTest(unittest.TestCase):
    def test_only_private_ipv4_counts_as_an_exit_resolver(self):
        self.assertEqual(listings.exit_dns({"dns": "10.195.0.53"}), "10.195.0.53")
        for bad in ({}, {"dns": None}, {"dns": "1.1.1.1"}, {"dns": "9.9.9.9"}, {"dns": "fd00::1"},
                    {"dns": "10.195.0.53:53"}, {"dns": 53}, None):
            self.assertIsNone(listings.exit_dns(bad), bad)


class KillSwitchTest(unittest.TestCase):
    def test_arm_disarm_dry(self):
        ks = killswitch.KillSwitch(name="srnet_ks_test")
        scripts = []
        def fake_f(script, check=True):
            scripts.append(script)
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        def fake(*a, check=True):
            scripts.append(" ".join(a))
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        ks._nft_f = fake_f  # type: ignore
        ks._nft = fake  # type: ignore
        ks.arm("eth0", bootstrap_ips=["1.2.3.4"])
        self.assertTrue(any("meta l4proto udp" in s for s in scripts))
        self.assertTrue(any("dport 53" in s for s in scripts))
        self.assertTrue(any('oifname "sr-tun0"' in s for s in scripts))
        ks.disarm()
        self.assertTrue(any("delete" in s for s in scripts))


class FlowReuseTest(unittest.TestCase):
    def test_connect_spend_after_map(self):
        b = MockBackend()
        r = connect(b, "S")
        self.assertTrue(r.ok)
        self.assertEqual(b.log, ["reserve", "map", "capable?", "commit"])
        self.assertTrue(b.spent)

    def test_failed_map_releases(self):
        b = MockBackend(map_ok=False)
        r = connect(b, "S")
        self.assertFalse(r.ok)
        self.assertFalse(b.spent)
        self.assertIn("release", b.log)


class ChainRefuseTest(unittest.TestCase):
    def test_mainnet_refused(self):
        with self.assertRaises(SystemExit):
            chain.refuse_mainnet(8453)


class BuyMockTest(unittest.TestCase):
    def test_buy_writes_claim(self):
        from srnet.cli import main
        doc, seller, d = _listing_doc()
        board = d / "board.json"
        board.write_text(json.dumps({"listings": [doc]}))
        home = d / "home"
        # ack first
        (home).mkdir()
        (home / "visibility.ack").write_text("I UNDERSTAND\n")
        rc = main(["--home", str(home), "buy", seller, "--listings", str(board),
                   "--client-sesh", "clientfixture00000000000000000000000000000000003.sesh",
                   "--mock"])
        self.assertEqual(rc, 0)
        st = json.loads((home / "state.json").read_text())
        self.assertEqual(st["pending_buy"]["seller"], seller)
        claim = Path(st["pending_buy"]["claim_path"])
        self.assertTrue(claim.exists())
        self.assertEqual(json.loads(claim.read_text())["v"], 1)

    def _buy(self, price, hours):
        from srnet.cli import main
        doc, seller, d = _listing_doc(price)
        board = d / "board.json"
        board.write_text(json.dumps({"listings": [doc]}))
        home = d / "home"; home.mkdir()
        (home / "visibility.ack").write_text("I UNDERSTAND\n")
        rc = main(["--home", str(home), "buy", seller, "--listings", str(board), "--hours", str(hours),
                   "--client-sesh", "clientfixture00000000000000000000000000000000003.sesh", "--mock"])
        return rc, home

    def test_hourly_amount_is_rate_times_hours(self):
        rc, home = self._buy("0.50", 6)
        self.assertEqual(rc, 0)
        p = json.loads((home / "state.json").read_text())["pending_buy"]
        self.assertEqual((p["rate_per_hour"], p["hours"], p["amount"]), (500_000, 6, 3_000_000))

    def test_over_cap_or_zero_hours_refused(self):
        with self.assertRaises(SystemExit):
            self._buy("5.00", 5)  # $25 > $20 cap
        with self.assertRaises(SystemExit):
            self._buy("1.00", 0)


class FinalPaymentAndRenewTest(unittest.TestCase):
    def test_failed_exit_sends_nothing_and_has_no_refund(self):
        ch = chain.MockChain()
        a = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        b = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        ch.release_failed(a)   # exit did not work -> nothing on chain (payment final), stop renewing
        ch.settle_success(b)
        self.assertTrue(ch.hold_info(a)[4], "lease stays paid/active: there is no buyer cancel")
        self.assertEqual(ch.released, {a})
        self.assertFalse(hasattr(chain.ChainCfg, "cancel"), "client has no buyer cancel")

    def test_buy_defaults_to_one_hour_with_auto_renew(self):
        rc, home = BuyMockTest._buy(BuyMockTest(), "1.00", 1)
        st = json.loads((home / "state.json").read_text())
        self.assertEqual(st["pending_buy"]["hours"], 1)
        self.assertTrue(st["lease"]["auto_renew"])

    def _st(self, ch, lease_id):
        d = Path(tempfile.mkdtemp())
        s = state.State(d)
        s.data["lease"] = {"seller": "x", "contract": ch.contract, "chainid": 31337, "current": lease_id,
                           "renewed_from": [], "auto_renew": True,
                           "client_sesh": "clientfixture00000000000000000000000000000000003.sesh",
                           "spool": str(d / "spool"), "rate_per_hour": 500_000}
        s.save()
        return s, d

    def test_hourly_renew_in_last_window_drops_new_claim(self):
        from srnet.cli import renew_once
        ch = chain.MockChain()
        a = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        s, d = self._st(ch, a)
        r = renew_once(ch, s)
        self.assertFalse(r["renewed"])
        self.assertTrue(r["reason"].startswith("wait:"))
        ch.clock += 3600 - 300  # 5 minutes left
        r = renew_once(ch, s)
        self.assertTrue(r["renewed"], r)
        new = r["lease_id"]
        self.assertEqual(ch.holds[new]["start"], ch.holds[a]["end"], "renewal starts exactly at the previous end")
        self.assertEqual(ch.holds[new]["rate"], 500_000)
        self.assertTrue((d / "spool" / f"hold-{new}.json").exists())
        self.assertEqual(renew_once(ch, s)["renewed"], False)  # next renewal not due yet

    def test_no_renew_after_seller_stop_or_when_off(self):
        from srnet.cli import renew_once
        ch = chain.MockChain()
        a = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        s, _ = self._st(ch, a)
        ch.seller_cancel(a)
        ch.clock += 3500
        self.assertEqual(renew_once(ch, s)["reason"], "seller_stopped")
        b = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        s2, _ = self._st(ch, b)
        s2.data["lease"]["auto_renew"] = False
        ch.clock += 3500
        self.assertEqual(renew_once(ch, s2)["reason"], "auto_renew_off")

    def test_backend_release_turns_auto_renew_off(self):
        from srnet.backend import LiveBackend
        ch = chain.MockChain()
        a = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        s, d = self._st(ch, a)
        b = LiveBackend(rpc=None, chain=ch, state=s, client_sesh="c.sesh", spool=d / "spool")
        b.release(str(a))
        self.assertFalse(s.data["lease"]["auto_renew"])


if __name__ == "__main__":
    unittest.main()
