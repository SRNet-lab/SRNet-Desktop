"""Store payments: the core shows the seller's address, the exact amount and a QR code (EIP-681),
and never sends or checks a payment itself. No chain or wallet needed."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]
import mbfmt as F  # noqa: E402
from typed import payinfo as PI, testbed as T, views  # noqa: E402
from typed.core import Core  # noqa: E402

PAY_TO = "0x" + "ab" * 20
TX = "0x" + "cd" * 32


class ShopPay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seller_seed = b"s" * 32
        cls.seller_pub = F.pub_hex(F.key_from_seed(seller_seed))
        cls.env = T.start(mirror=False, sellers=[cls.seller_pub],
                          hub_override={"shop": {"new_key_rate": [[50, 3600]]}}, extra_shops=("store2",))
        cls.cfg = T.client_config(cls.env)
        d = cls.env["tmp"] / "pay"
        (d / "seller").mkdir(parents=True)
        (d / "seller" / "author.key").write_bytes(seller_seed)
        cls.seller = Core(str(d / "seller"), cls.cfg, autoconfirm=True)
        cls.seller.sync()
        (d / "buyer").mkdir(parents=True)
        cls.buyer = Core(str(d / "buyer"), cls.cfg, autoconfirm=True)
        cls.buyer.sync()

    @classmethod
    def tearDownClass(cls):
        T.stop(cls.env)

    def listing(self, title, price="2.5", **kw):
        body = dict({"title": title, "desc": "", "price": price, "currency": "USDC", "chain_id": 8453,
                     "pay_to": PAY_TO}, **kw)
        r = self.seller.draft("store", "shop.listing", body, [])["result"]
        self.buyer.sync("store")
        found = [l for l in self.buyer.view("store")["view"]["listings"] if l["title"] == title]
        return r, (found[0] if found else None)

    def test_usdc_sheet_shows_address_amount_and_link(self):
        r, l = self.listing("Stickers")
        self.assertTrue(r["ok"], r)
        self.assertTrue(l["payable"])
        d = self.buyer.pay_draft("store", l["id"])
        s = d["sheet"]
        self.assertEqual((s["to"], s["currency"], s["chain_id"], s["network"]), (PAY_TO, "USDC", 8453, "Base"))
        units = PI.usdc_units(s["amount"])
        self.assertTrue(2_500_000 < units <= 2_500_000 + PI.ORDER_ID_MAX, s["amount"])   # this order's own amount
        self.assertEqual(s["token_address"], PI.USDC[8453])
        self.assertEqual(s["uri"], f"ethereum:{PI.USDC[8453]}@8453/transfer?address={PAY_TO}&uint256={units}")
        self.assertNotEqual({self.buyer.pay_draft("store", l["id"])["sheet"]["amount"] for _ in range(5)}, {s["amount"]})
        self.assertIn("never sends funds", s["note"])
        if PI.have_qr():
            self.assertTrue(self.buyer.pay_qr(d["token"]).lstrip().startswith(b"<"))

    def test_arbitrum_and_limit(self):
        _, l = self.listing("Poster", price="7", chain_id=42161)
        self.assertIn("@42161/transfer", self.buyer.pay_draft("store", l["id"])["sheet"]["uri"])
        _, big = self.listing("Big thing", price="25")
        with self.assertRaises(F.FormatError) as e:
            self.buyer.pay_draft("store", big["id"])
        self.assertIn("over your limit of 20.00", str(e.exception))
        rich = Core(str(self.env["tmp"] / "pay" / "rich"), dict(self.cfg, payments={"max_usdc": "50"}),
                    autoconfirm=True)
        rich.sync()
        self.assertTrue(rich.pay_draft("store", big["id"])["sheet"]["amount"].startswith("25.00"))

    def test_host_refuses_bad_listings(self):
        for title, kw in (("old style", {"currency": "SRNET", "vendor_id": "0x" + "22" * 32, "chain_id": None,
                                         "pay_to": None}),
                          ("wrong chain", {"chain_id": 1}),
                          ("eth mainnet", {"currency": "ETH", "chain_id": 42161}),
                          ("too precise", {"price": "1.0000001"})):
            body = {k: v for k, v in dict({"title": title, "desc": "", "price": "1", "currency": "USDC",
                                           "chain_id": 8453, "pay_to": PAY_TO}, **kw).items() if v is not None}
            r = self.seller.draft("store", "shop.listing", body, [])["result"]
            self.assertFalse(r.get("ok"), (title, r))

    def test_old_srnet_listing_shows_seller_needs_update(self):
        items = [{"t": "shop.listing", "author": "aa" * 32, "id": "x1", "ts": 1, "refs": [],
                  "body": {"title": "Old", "price": "1", "currency": "SRNET", "vendor_id": "0x" + "22" * 32}},
                 {"t": "shop.order", "author": "bb" * 32, "id": "o1", "ts": 2, "refs": ["x1"],
                  "body": {"lease_id": 5, "buyer": PAY_TO}}]
        v = views.shop_view(items, {"sellers": ["aa" * 32]})
        l = v["listings"][0]
        self.assertFalse(l["payable"])
        self.assertEqual(l["note"], "seller needs to update")
        self.assertEqual(l["orders"][0]["lease_id"], 5)

    def test_order_names_the_tx_and_verify_without_addon(self):
        _, l = self.listing("Mug")
        d = self.buyer.pay_draft("store", l["id"])
        with self.assertRaises(F.FormatError):
            self.buyer.pay_order(d["token"], "0x1234")
        r = self.buyer.pay_order(d["token"], TX, "thanks")["result"]
        self.assertTrue(r["ok"], r)
        self.seller.sync("store")
        mine = next(x for x in self.seller.view("store")["view"]["listings"] if x["id"] == l["id"])
        o = mine["orders"][0]
        self.assertEqual((o["chain_id"], o["tx"], o["note"], o["amount"]), (8453, TX, "thanks", d["sheet"]["amount"]))
        self.seller.addons = {}
        v = self.seller.verify_order("store", o["id"])
        self.assertFalse(v["checked"])
        self.assertEqual(v["link"], "https://basescan.org/tx/" + TX)
        self.assertIn("exits add-on", v["note"])
        self.assertIn(f"exactly {d['sheet']['amount']} USDC", v["note"])

    def test_verify_goes_to_the_exits_addon(self):
        _, l = self.listing("Hat")
        d = self.buyer.pay_draft("store", l["id"])
        self.buyer.pay_order(d["token"], "0x" + "e1" * 32)
        self.seller.sync("store")
        o = next(x for x in self.seller.view("store")["view"]["listings"] if x["id"] == l["id"])["orders"][0]
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen.append((self.path, self.headers.get("X-CSRF-Token"),
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = json.dumps({"ok": True, "paid": True, "amount": "2.50"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.seller.addons = {"exits": {"title": "Exits", "base": f"http://127.0.0.1:{srv.server_address[1]}",
                                            "tok": "t" * 20, "slow": set()}}
            v = self.seller.verify_order("store", o["id"])
        finally:
            srv.shutdown()
            srv.server_close()
            self.seller.addons = {}
        self.assertTrue(v["paid"] and v["checked"])
        self.assertEqual(seen[0][:2], ("/api/verify", "t" * 20))
        self.assertEqual(seen[0][2], {"chain_id": 8453, "tx": "0x" + "e1" * 32, "to": PAY_TO,
                                      "amount": d["sheet"]["amount"]})

    def test_copied_tx_hash_does_not_make_another_order_paid(self):
        _, l = self.listing("Pin")
        tx = "0x" + "f2" * 32
        a = self.buyer.pay_draft("store", l["id"])
        self.buyer.pay_order(a["token"], tx)
        thief = Core(str(self.env["tmp"] / "pay" / "thief"), self.cfg, autoconfirm=True)
        thief.sync()
        b = thief.pay_draft("store", l["id"])
        thief.pay_order(b["token"], tx)
        self.seller.sync("store")
        orders = sorted(next(x for x in self.seller.view("store")["view"]["listings"] if x["id"] == l["id"])["orders"],
                        key=lambda o: (o["ts"], o["id"]))
        self.assertEqual(len(orders), 2)
        self.seller.addons = {"exits": {"title": "Exits", "base": "http://127.0.0.1:9", "tok": "t" * 20, "slow": set()}}
        try:
            first, second = (self.seller.verify_order("store", o["id"]) for o in orders)
        finally:
            self.seller.addons = {}
        self.assertFalse(second["paid"])
        self.assertIn("already named by order " + orders[0]["id"], second["why"])
        self.assertFalse(first["paid"])              # in dispute: the seller asks both buyers
        self.assertIn("also named by order", first["why"])

    def test_same_tx_named_in_another_store_paid_to_the_same_address(self):
        _, l1 = self.listing("Mug")
        r = self.seller.draft("store2", "shop.listing", {"title": "Mug too", "desc": "", "price": "2.5",
                                                          "currency": "USDC", "chain_id": 8453, "pay_to": PAY_TO}, [])
        self.assertTrue(r["result"]["ok"], r)
        self.buyer.sync("store2")
        l2 = next(x for x in self.buyer.view("store2")["view"]["listings"] if x["title"] == "Mug too")
        tx = "0x" + "a7" * 32
        a = self.buyer.pay_draft("store", l1["id"])
        self.buyer.pay_order(a["token"], tx)
        thief = Core(str(self.env["tmp"] / "pay" / "thief2"), self.cfg, autoconfirm=True)
        thief.sync()
        b = thief.pay_draft("store2", l2["id"])
        thief.pay_order(b["token"], tx)
        self.seller.sync()
        o2 = next(x for x in self.seller.view("store2")["view"]["listings"] if x["id"] == l2["id"])["orders"][0]
        self.seller.addons = {"exits": {"title": "Exits", "base": "http://127.0.0.1:9", "tok": "t" * 20, "slow": set()}}
        try:
            v = self.seller.verify_order("store2", o2["id"])
        finally:
            self.seller.addons = {}
        self.assertFalse(v["paid"])
        self.assertIn("named by order", v["why"])

    def test_payment_sheet_survives_an_app_restart(self):
        _, l = self.listing("Cap")
        d = self.buyer.pay_draft("store", l["id"])
        again = Core(str(self.env["tmp"] / "pay" / "buyer"), self.cfg, autoconfirm=True)
        r = again.pay_order(d["token"], "0x" + "b8" * 32)["result"]
        self.assertTrue(r["ok"], r)
        again.sync("store")
        mine = [o for x in again.view("store")["view"]["listings"] if x["id"] == l["id"] for o in x["orders"]]
        self.assertEqual(mine[0].get("amount"), d["sheet"]["amount"])
        p = self.env["tmp"] / "pay" / "buyer" / "pending_pay.json"
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_order_on_another_chain_or_amount_is_not_paid(self):
        items = [{"t": "shop.listing", "author": "aa" * 32, "id": "x1", "ts": 1, "refs": [],
                  "body": {"title": "Cap", "price": "1", "currency": "USDC", "chain_id": 8453, "pay_to": PAY_TO}},
                 {"t": "shop.order", "author": "bb" * 32, "id": "o1", "ts": 2, "refs": ["x1"],
                  "body": {"chain_id": 42161, "tx": TX}},
                 {"t": "shop.order", "author": "bb" * 32, "id": "o2", "ts": 3, "refs": ["x1"],
                  "body": {"chain_id": 8453, "tx": "0x" + "01" * 32, "amount": "5.00"}}]
        c = type("C", (), {"policy": {"sellers": ["aa" * 32]}})()
        core = Core.__new__(Core)
        core.colls, core.addons = {"s": c}, {"exits": {}}
        core._items = lambda _c: (items, None)
        v = core.verify_order("s", "o1")
        self.assertFalse(v["paid"])
        self.assertIn("names Arbitrum One, but the listing takes payment on Base", v["why"])
        v = core.verify_order("s", "o2")
        self.assertFalse(v["paid"])
        self.assertIn("isn't this listing's price", v["why"])


class EthIsTestnetOnly(unittest.TestCase):
    def test_no_store_policy_turns_on_mainnet_eth(self):
        host = type("H", (), {"policy": {"sellers": ["aa" * 32], "allow_mainnet": True}})()
        meta = type("M", (), {"t": "shop.listing", "author": "aa" * 32,
                              "body": {"title": "t", "desc": "", "price": "0.01", "currency": "ETH",
                                       "chain_id": 42161, "pay_to": PAY_TO}})()
        self.assertEqual(views.host_rules(host, meta), "ETH payments are testnet-only")
        meta.body = dict(meta.body, chain_id=84532)
        self.assertIsNone(views.host_rules(host, meta))


class PayInfo(unittest.TestCase):
    def test_order_amounts(self):
        self.assertEqual(PI.order_units(1_000_000, rng=lambda n: 0), 1_000_001)
        self.assertEqual(PI.order_units(1_000_000, rng=lambda n: n - 1), 1_000_000 + PI.ORDER_ID_MAX)
        self.assertTrue(PI.order_amount_ok(1_000_000, 1_000_001))
        self.assertFalse(PI.order_amount_ok(1_000_000, 1_000_000))
        self.assertFalse(PI.order_amount_ok(1_000_000, 1_010_000))

    def test_amounts(self):
        self.assertEqual(PI.usdc_units("2.5"), 2_500_000)
        self.assertEqual(PI.fmt_usdc(3_000_417), "3.000417")
        self.assertEqual(PI.fmt_usdc(20_000_000), "20.00")
        for bad in ("0", "-1", "1.1234567", "abc", ""):
            with self.assertRaises(PI.PayError):
                PI.usdc_units(bad)
        self.assertEqual(PI.max_units({}), 20_000_000)
        self.assertEqual(PI.max_units({"payments": {"max_usdc": "5"}}), 5_000_000)
        with self.assertRaises(PI.PayError):
            PI.usdc_link(1, PAY_TO, 1)


if __name__ == "__main__":
    unittest.main()
