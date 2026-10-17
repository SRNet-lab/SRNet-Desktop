"""Store purchase paid from SRNet balance, on local anvil only (skipped without Foundry)."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(ROOT / "scripts"), str(ROOT / "client-cli")]
import anvil_env  # noqa: E402
from typed import testbed as T  # noqa: E402
from typed.core import Core  # noqa: E402


@unittest.skipUnless(anvil_env.available(), "anvil/forge not installed")
class ShopPay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from srnet import chain, payments
        import mbfmt as F
        cls.chain = anvil_env.start()
        seller_seed = b"s" * 32
        cls.seller_pub = F.pub_hex(F.key_from_seed(seller_seed))
        cls.env = T.start(mirror=False, sellers=[cls.seller_pub])
        cfg = T.client_config(cls.env)
        cfg["payments"] = {"rpc": cls.chain["rpc"], "contract": cls.chain["contract"]}
        cls.cfg = cfg
        # seller wallet (dev key 3) binds its vendor id on-chain, derived from the store seller key
        k = cls.chain["keys"]
        sch = chain.ChainCfg(rpc=cls.chain["rpc"], contract=cls.chain["contract"], private_key=k[3])
        salt = payments.shop_salt(cls.seller_pub)
        cls.vid = payments.vendor_id_for(sch.address(), salt)
        sch._send("configureVendor(bytes32,bytes32)", cls.vid, salt)
        cls.sch = sch
        # buyer (dev key 4) has 30 USDC in SRNet balance
        bch = chain.ChainCfg(rpc=cls.chain["rpc"], contract=cls.chain["contract"], private_key=k[4])
        bch._send("mint(address,uint256)", bch.address(), 50_000_000, to=bch.usdc())
        bch.approve(50_000_000); bch.fund(30_000_000)
        cls.bch = bch
        d = cls.env["tmp"] / "pay"
        (d / "seller").mkdir(parents=True); (d / "seller" / "author.key").write_bytes(seller_seed)
        cls.seller = Core(str(d / "seller"), cfg, autoconfirm=True); cls.seller.sync()
        (d / "buyer").mkdir(parents=True)
        cls.buyer = Core(str(d / "buyer"), cfg, autoconfirm=True); cls.buyer.sync()

    @classmethod
    def tearDownClass(cls):
        anvil_env.stop(cls.chain)

    def listing(self, price="2.5", **kw):
        body = dict({"title": "Sticker pack", "desc": "", "price": price, "currency": "SRNET",
                     "vendor_id": self.vid}, **kw)
        r = self.seller.draft("store", "shop.listing", body, [])["result"]
        self.buyer.sync("store")
        return r, next(l for l in self.buyer.view("store")["view"]["listings"] if l["title"] == body["title"]
                       and l["price"] == price)

    def test_pay_with_srnet_balance(self):
        r, l = self.listing()
        self.assertTrue(r["ok"], r)
        before = self.bch.available()
        d = self.buyer.pay_draft("store", l["id"])
        self.assertTrue(d["srnet"])
        self.assertIn("no extra fee", "\n".join(d["sheet"]["lines"]))
        res = self.buyer.pay_confirm(d["token"], private_key=self.chain["keys"][4])
        self.assertTrue(res["ok"], res)
        self.assertEqual(before - self.bch.available(), 2_500_000)  # no fee at payment time
        self.assertTrue(res["receipt_posted"])
        self.seller.sync("store")
        o = [x for x in self.seller.view("store")["view"]["listings"] if x["id"] == l["id"]][0]["orders"][0]
        self.assertTrue(self.seller.verify_order("store", o["id"])["paid"])
        self.assertEqual(self.sch.earned(res["lease_id"]), 2_500_000)  # seller can claim it all now
        # token is single use
        self.assertFalse(self.buyer.pay_confirm(d["token"], private_key=self.chain["keys"][4])["ok"])

    def test_cap_and_validation(self):
        bad = self.seller.draft("store", "shop.listing", {"title": "Too much", "price": "25", "currency": "SRNET",
                                                          "vendor_id": self.vid}, [])["result"]
        self.assertFalse(bad["ok"])  # over the 20 USDC per-payment cap: host refuses
        nov = self.seller.draft("store", "shop.listing", {"title": "No vendor", "price": "1", "currency": "SRNET"},
                                [])["result"]
        self.assertFalse(nov["ok"])

    def test_insufficient_balance(self):
        from srnet import chain
        k = self.chain["keys"][2]  # funded with nothing in SRNet balance
        _, l = self.listing(price="3", title="Poster")
        d = self.buyer.pay_draft("store", l["id"])
        res = self.buyer.pay_confirm(d["token"], private_key=k)
        self.assertFalse(res["ok"])
        self.assertIn("balance", res["error"])


if __name__ == "__main__":
    unittest.main()
