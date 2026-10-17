import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor"), str(ROOT.parent / "scripts")]
from srnet import chain, payments as P  # noqa: E402

VID = "0x" + "11" * 32


class Unit(unittest.TestCase):
    def test_usdc_parse(self):
        self.assertEqual(P.usdc("2.5"), 2_500_000)
        self.assertEqual(P.usdc("20"), 20_000_000)
        for bad in ("-1", "1.1234567", "abc", ""):
            with self.assertRaises(P.PaymentError):
                P.usdc(bad)

    def test_cap_and_checks(self):
        P.Payment("shop", VID, 20_000_000).check()
        with self.assertRaises(P.PaymentError):
            P.Payment("shop", VID, 20_000_001).check()
        with self.assertRaises(P.PaymentError):
            P.Payment("exit", VID, 5_000_000, hours=5).check()
        with self.assertRaises(P.PaymentError):
            P.Payment("shop", "0x12", 1).check()
        with self.assertRaises(P.PaymentError):
            P.Payment("tip", VID, 1).check()

    def test_describe_says_final_and_no_fee(self):
        t = "\n".join(P.describe(P.Payment("shop", VID, 1_250_000, item="Poster", seller="ann·abc123")))
        self.assertIn("1.25 USDC", t); self.assertIn("final", t); self.assertIn("no extra fee", t)

    def test_mock_chain_pay(self):
        ch = chain.MockChain(chainid=31337, contract="0x" + "22" * 20)
        ch.fund(10_000_000)
        r = P.pay(ch, P.Payment("shop", VID, 3_000_000))
        self.assertTrue(r["ok"]); self.assertGreater(r["lease_id"], 0)

    def test_vendor_id_matches_contract_rule(self):
        from eth_utils import keccak
        w = "0x" + "ab" * 20
        s = P.shop_salt("cd" * 32)
        self.assertEqual(P.vendor_id_for(w, s), "0x" + keccak(bytes.fromhex("ab" * 20) + bytes.fromhex(s[2:])).hex())


try:
    import anvil_env
    HAVE = anvil_env.available()
except Exception:
    HAVE = False


@unittest.skipUnless(HAVE, "anvil/forge not installed")
class Anvil(unittest.TestCase):
    def test_exit_and_shop_share_one_flow(self):
        env = anvil_env.start()
        try:
            k = env["keys"]
            seller = chain.ChainCfg(rpc=env["rpc"], contract=env["contract"], private_key=k[1])
            salt = P.shop_salt("ee" * 32)
            vid = P.vendor_id_for(seller.address(), salt)
            seller._send("configureVendor(bytes32,bytes32)", vid, salt)
            buyer = chain.ChainCfg(rpc=env["rpc"], contract=env["contract"], private_key=k[2])
            buyer._send("mint(address,uint256)", buyer.address(), 40_000_000, to=buyer.usdc())
            buyer.approve(40_000_000); buyer.fund(40_000_000)
            self.assertEqual(buyer.available(), 38_800_000)  # 3% taken once, at deposit
            a = P.pay(buyer, P.Payment("exit", vid, 500_000, hours=2))
            b = P.pay(buyer, P.Payment("shop", vid, 7_000_000))
            self.assertEqual(buyer.available(), 38_800_000 - 1_000_000 - 7_000_000)
            self.assertTrue(P.verify(seller, b["lease_id"], vid, 7_000_000, buyer.address()))
            self.assertFalse(P.verify(seller, b["lease_id"], vid, 7_000_001))
            self.assertFalse(P.verify(seller, a["lease_id"], "0x" + "99" * 32, 1))
            with self.assertRaises(chain.ChainError):
                P.pay(buyer, P.Payment("shop", "0x" + "77" * 32, 1_000_000))  # unconfigured vendor
        finally:
            anvil_env.stop(env)

    def test_gasless_buy_renew_cashout_with_zero_eth(self):
        """QR/external-wallet path: buyer holds USDC but no ETH; relayer pays all gas."""
        import secrets as _s
        from srnet import gasless, relayer as R
        env = anvil_env.start()
        try:
            k = env["keys"]
            seller = chain.ChainCfg(rpc=env["rpc"], contract=env["contract"], private_key=k[1])
            salt = P.shop_salt("ee" * 32)
            vid = P.vendor_id_for(seller.address(), salt)
            seller._send("configureVendor(bytes32,bytes32)", vid, salt)
            rel_ch = chain.ChainCfg(rpc=env["rpc"], contract=env["contract"], private_key=k[3])
            srv = R.serve(R.Relayer(rel_ch, min_fee=50_000, min_action_fee=20_000, max_gas_per_tx=10**17, gas_budget_wei=10**18), "127.0.0.1", 0)
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            try:
                fresh = "0x" + _s.token_hex(32)  # brand-new key: zero ETH
                buyer = chain.ChainCfg(rpc=env["rpc"], contract=env["contract"], private_key=fresh, relayer=url)
                self.assertEqual(buyer.eth_balance(), 0)
                seller._send("mint(address,uint256)", buyer.address(), 10_000_000, to=buyer.usdc())  # "QR send"
                n, name, ver = buyer.permit_info()
                req = gasless.sign_topup(buyer._acct, chainid=buyer.chainid(), contract=buyer.contract,
                                         usdc=buyer.usdc(), amount=10_000_000, permit_nonce=n,
                                         usdc_name=name, usdc_version=ver)
                self.assertTrue(gasless.submit(url, req)["ok"])
                avail = buyer.available()
                self.assertEqual(avail, 10_000_000 - 300_000 - 50_000)
                a = P.pay(buyer, P.Payment("exit", vid, 1_000_000))
                self.assertEqual(buyer.available(), avail - 1_000_000 - 20_000)
                self.assertTrue(P.verify(seller, a["lease_id"], vid, 1_000_000, buyer.address()))
                buyer._rpc.call("evm_increaseTime", 3300)
                buyer._rpc.call("evm_mine")
                r = buyer.renew(a["lease_id"], 1)  # hourly auto-renew, also gasless
                self.assertEqual(buyer.renewed_by(a["lease_id"]), r)
                b = P.pay(buyer, P.Payment("shop", vid, 2_000_000))
                self.assertGreater(b["lease_id"], r)
                left = buyer.available()
                buyer.cash_out(1_000_000)
                self.assertEqual(buyer.available(), left - 1_000_000 - 20_000)
                self.assertEqual(buyer.usdc_balance(), 1_000_000)
                self.assertEqual(buyer.eth_balance(), 0)  # never needed gas
            finally:
                srv.shutdown()
        finally:
            anvil_env.stop(env)


if __name__ == "__main__":
    unittest.main()
