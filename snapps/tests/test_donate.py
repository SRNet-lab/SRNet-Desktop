"""Donation page: custom amounts, unique amounts, and on-chain checks against a fake chain.
No network and no funds needed."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent)]
from donate import pay as P, server as S  # noqa: E402

TO = "0xec4fEECa354C7eabbe8957a23c361Aa365E3b377"
OTHER = "0x" + "12" * 20
SENDER = "0x" + "c0" * 20
T0 = 1_800_000_000


def h(n: int) -> str:
    return hex(n)


class FakeChain:
    """Just the JSON-RPC reads the checker makes."""

    def __init__(self, chain_id, head=1000):
        self.chain_id, self.head = chain_id, head
        self.txs, self.receipts, self.blocks = {}, {}, {}
        self.down = False
        self.calls = 0

    def add_eth(self, tx, to, value, block=980, status=1, ts=T0 + 60, tx_chain=None, frm=SENDER):
        self.txs[tx] = {"hash": tx, "from": frm, "to": to, "value": h(value),
                        "chainId": h(tx_chain if tx_chain is not None else self.chain_id)}
        self._mine(tx, block, status, ts, [])

    def add_usdc(self, tx, token, to, value, block=980, ts=T0 + 60):
        self.txs[tx] = {"hash": tx, "from": SENDER, "to": token, "value": "0x0", "chainId": h(self.chain_id)}
        log = {"address": token, "topics": [P.TRANSFER_TOPIC, "0x" + "00" * 12 + SENDER[2:],
                                            "0x" + "00" * 12 + to.lower()[2:]], "data": h(value)}
        self._mine(tx, block, 1, ts, [log])

    def _mine(self, tx, block, status, ts, logs):
        bh = "0x" + f"{block:064x}"
        self.receipts[tx] = {"transactionHash": tx, "blockNumber": h(block), "blockHash": bh,
                             "status": h(status), "logs": logs}
        self.blocks[block] = {"hash": bh, "timestamp": h(ts), "number": h(block)}

    def call(self, method, *params):
        self.calls += 1
        if self.down:
            raise P.RpcError("down")
        if method == "eth_chainId":
            return h(self.chain_id)
        if method == "eth_blockNumber":
            return h(self.head)
        if method == "eth_getTransactionByHash":
            return self.txs.get(params[0])
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getBlockByNumber":
            return self.blocks.get(int(params[0], 16))
        raise P.RpcError("unknown method " + method)


def tx(n: int) -> str:
    return "0x" + f"{n:064x}"


def donation(asset="eth", chain=1, units=300_000_004_321_000, created=T0, t=tx(1)):
    return {"asset": asset, "chain": chain, "units": units, "created": created, "tx": t}


class Amounts(unittest.TestCase):
    def test_bounds_and_format(self):
        self.assertEqual(P.parse_amount("0.10", "usdc"), 100_000)
        self.assertEqual(P.parse_amount("10000", "usdc"), 10_000_000_000)
        self.assertEqual(P.parse_amount("2,5", "usdc"), 2_500_000)
        self.assertEqual(P.parse_amount("0.0003", "eth"), 300_000_000_000_000)
        self.assertEqual(P.parse_amount("5", "eth"), 5 * 10**18)
        for bad, asset in (("0.09", "usdc"), ("10000.01", "usdc"), ("1.001", "usdc"), ("-1", "usdc"),
                           ("1e3", "usdc"), ("", "usdc"), ("abc", "usdc"), ("0x10", "usdc"),
                           ("0.000009", "eth"), ("5.000001", "eth"), ("0.0000001", "eth"),
                           ("NaN", "eth"), ("1", "dai"), (" 1 1", "usdc")):
            with self.assertRaises(P.AmountError, msg=(bad, asset)):
                P.parse_amount(bad, asset)

    def test_unique_amount_is_close_and_skips_taken(self):
        base = P.parse_amount("0.11", "usdc")
        u = P.unique_units("usdc", base, set())
        self.assertTrue(base < u <= base + P.TAG_MAX)
        e = P.parse_amount("0.0003", "eth")
        ue = P.unique_units("eth", e, set())
        self.assertTrue(e < ue <= e + P.TAG_MAX * 10**9)
        self.assertEqual((ue - e) % 10**9, 0)
        # a rigged random source that always lands on a taken amount falls back to a free one
        taken = {base + i for i in range(1, P.TAG_MAX)}
        self.assertEqual(P.unique_units("usdc", base, taken, rng=lambda n: 0 if n == P.TAG_MAX else n - 1),
                         base + P.TAG_MAX)
        with self.assertRaises(P.AmountError):
            P.unique_units("usdc", base, taken | {base + P.TAG_MAX})

    def test_format_and_uri(self):
        self.assertEqual(P.fmt_units(110_423, "usdc"), "0.110423")
        self.assertEqual(P.fmt_units(1_000_000, "usdc"), "1.00")
        self.assertEqual(P.fmt_units(300_004_321_000_000, "eth"), "0.000300004321")
        self.assertEqual(P.pay_uri("eth", 1, TO, 300_004_321_000_000),
                         f"ethereum:{TO}@1?value=300004321000000")
        self.assertEqual(P.pay_uri("usdc", 8453, TO, 110_423),
                         f"ethereum:{P.USDC_TOKENS[8453]}@8453/transfer?address={TO}&uint256=110423")
        self.assertFalse(P.offered("usdc", 1))
        self.assertTrue(P.offered("eth", 1))


class EthCheck(unittest.TestCase):
    def setUp(self):
        self.a, self.b = FakeChain(1), FakeChain(1)
        self.d = donation()

    def both(self, fn, *args, **kw):
        for c in (self.a, self.b):
            getattr(c, fn)(*args, **kw)

    def test_paid(self):
        self.both("add_eth", tx(1), TO.lower(), self.d["units"])
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "paid", r)
        self.assertEqual(r["from"], SENDER)
        self.assertEqual(r["sources"], 2)

    def test_recipient_checksum_case_doesnt_matter(self):
        self.both("add_eth", tx(1), TO, self.d["units"])
        self.assertEqual(P.check([self.a, self.b], self.d, TO.lower())["state"], "paid")

    def test_wrong_recipient(self):
        self.both("add_eth", tx(1), OTHER, self.d["units"])
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "bad")
        self.assertIn("does not send ETH to the donation address", r["why"])

    def test_underpaid_and_overpaid(self):
        self.both("add_eth", tx(1), TO, self.d["units"] - 1)
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "bad")
        self.assertIn("underpaid", r["why"])
        self.both("add_eth", tx(1), TO, self.d["units"] + 10**9)
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "bad")
        self.assertIn("does not match", r["why"])

    def test_wrong_chain(self):
        # the RPCs are on Arbitrum, the donation is on Ethereum
        a, b = FakeChain(42161), FakeChain(42161)
        for c in (a, b):
            c.add_eth(tx(1), TO, self.d["units"])
        r = P.check([a, b], self.d, TO)
        self.assertEqual(r["state"], "pending")
        self.assertIn("couldn't reach", r["why"])
        # the transaction itself names another chain
        self.both("add_eth", tx(1), TO, self.d["units"], tx_chain=42161)
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "bad")
        self.assertIn("another chain", r["why"])
        # a transaction that only exists on another chain is simply not found here
        self.both("add_eth", tx(2), TO, self.d["units"])
        r = P.check([self.a, self.b], dict(self.d, tx=tx(3)), TO)
        self.assertEqual(r["state"], "pending")
        self.assertIn("not found", r["why"])

    def test_failed_tx(self):
        self.both("add_eth", tx(1), TO, self.d["units"], status=0)
        self.assertIn("failed", P.check([self.a, self.b], self.d, TO)["why"])

    def test_low_confirmations(self):
        self.both("add_eth", tx(1), TO, self.d["units"], block=995)   # head 1000 -> 6 of 12
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "pending")
        self.assertIn("6 of 12 confirmations", r["why"])
        for c in (self.a, self.b):
            c.head = 1006
        self.assertEqual(P.check([self.a, self.b], self.d, TO)["state"], "paid")

    def test_not_mined(self):
        self.both("add_eth", tx(1), TO, self.d["units"])
        for c in (self.a, self.b):
            c.receipts[tx(1)] = None
        self.assertIn("not mined", P.check([self.a, self.b], self.d, TO)["why"])

    def test_reorg(self):
        self.both("add_eth", tx(1), TO, self.d["units"])
        self.a.blocks[980]["hash"] = "0x" + "ee" * 32   # block 990 was replaced
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "pending")
        self.assertIn("reorg", r["why"])

    def test_rpcs_disagree_on_block(self):
        self.a.add_eth(tx(1), TO, self.d["units"], block=980)
        self.b.add_eth(tx(1), TO, self.d["units"], block=981)
        self.assertEqual(P.check([self.a, self.b], self.d, TO)["state"], "pending")

    def test_one_rpc_alone_cant_mark_paid(self):
        self.a.add_eth(tx(1), TO, self.d["units"])     # a lying (or lone) RPC says paid
        r = P.check([self.a, self.b], self.d, TO)      # the other has never seen it
        self.assertEqual(r["state"], "pending")
        self.b.down = True
        r = P.check([self.a, self.b], self.d, TO)      # the other is down: still not enough
        self.assertEqual(r["state"], "pending")
        self.assertEqual(P.check([self.a, self.b], self.d, TO, quorum=1)["state"], "paid")

    def test_sent_before_donation_opened(self):
        self.both("add_eth", tx(1), TO, self.d["units"], ts=T0 - P.EARLY_SLACK_S - 1)
        r = P.check([self.a, self.b], self.d, TO)
        self.assertEqual(r["state"], "bad")
        self.assertIn("before this donation was opened", r["why"])

    def test_rpc_answering_for_another_tx_is_ignored(self):
        self.both("add_eth", tx(1), TO, self.d["units"])
        self.a.txs[tx(1)] = dict(self.a.txs[tx(1)], hash=tx(9))
        self.assertEqual(P.check([self.a, self.b], self.d, TO)["state"], "pending")


class UsdcCheck(unittest.TestCase):
    def setUp(self):
        self.c = FakeChain(8453)
        self.d = donation("usdc", 8453, 110_423)

    def test_paid(self):
        self.c.add_usdc(tx(1), P.USDC_TOKENS[8453].lower(), TO, 110_423)   # logs use lower case
        self.assertEqual(P.check([self.c], self.d, TO)["state"], "paid")

    def test_fake_token_wrong_recipient_wrong_amount(self):
        self.c.add_usdc(tx(1), "0x" + "99" * 20, TO, 110_423)
        self.assertIn("no USDC transfer", P.check([self.c], self.d, TO)["why"])
        self.c.add_usdc(tx(1), P.USDC_TOKENS[8453], OTHER, 110_423)
        self.assertIn("no USDC transfer", P.check([self.c], self.d, TO)["why"])
        self.c.add_usdc(tx(1), P.USDC_TOKENS[8453], TO, 110_000)
        self.assertIn("underpaid", P.check([self.c], self.d, TO)["why"])


class AppFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [T0]
        self.chains = {c: [FakeChain(c), FakeChain(c)] for c in P.CHAINS}
        self.app = S.App(Path(self.tmp.name), TO, rpcs=self.chains, clock=lambda: self.now[0])

    def tearDown(self):
        self.tmp.cleanup()

    def pay(self, v, t, units=None):
        for c in self.chains[v["chain"]]:
            if v["asset"] == "eth":
                c.add_eth(t, TO, units or v["units"])
            else:
                c.add_usdc(t, P.USDC_TOKENS[v["chain"]], TO, units or v["units"])

    def test_open_donations_never_share_an_amount(self):
        seen = set()
        for i in range(40):
            v = self.app.create("usdc", 42161, "0.11", f"caller{i // 10}")
            self.assertNotIn(v["units"], seen)
            seen.add(v["units"])
        with self.assertRaises(P.AmountError):
            self.app.create("usdc", 1, "1", "x")        # USDC isn't offered on Ethereum here
        with self.assertRaises(P.AmountError):
            self.app.create("eth", 10, "1", "x")        # unknown chain

    def test_duplicate_tx_pays_only_one_donation(self):
        v1 = self.app.create("eth", 1, "0.0003", "a")
        v2 = self.app.create("eth", 1, "0.0003", "b")
        self.pay(v1, tx(1))
        self.assertEqual(self.app.check(v1["id"], tx(1), "a")["state"], "paid")
        self.assertTrue(self.app.check(v1["id"], tx(1), "a").get("already"))
        r = self.app.check(v2["id"], tx(1), "b")
        self.assertEqual(r["state"], "bad")
        self.assertIn("already used", r["why"])
        # upper-case hash is the same transaction
        self.assertIn("already used", self.app.check(v2["id"], tx(1).upper().replace("0X", "0x"), "b")["why"])
        # and the record survives a restart
        app2 = S.App(Path(self.tmp.name), TO, rpcs=self.chains, clock=lambda: self.now[0])
        self.assertEqual(app2.ledger.get(v1["id"])["status"], "paid")
        self.assertIn("already used", app2.check(v2["id"], tx(1), "b")["why"])

    def test_ledger_refuses_to_forget(self):
        v1 = self.app.create("eth", 1, "0.0003", "a")
        v2 = self.app.create("eth", 1, "0.0003", "b")
        self.assertEqual(self.app.ledger.mark_paid(v1["id"], tx(8), {})["state"], "paid")
        self.assertIn("already used", self.app.ledger.mark_paid(v2["id"], tx(8), {})["why"])
        (Path(self.tmp.name) / "donations.json").write_text("{broken")
        with self.assertRaises(SystemExit):
            S.App(Path(self.tmp.name), TO, rpcs=self.chains)

    def test_other_donations_tx_cant_be_claimed(self):
        # someone else's earlier payment of a different amount doesn't pay this donation
        v1 = self.app.create("usdc", 8453, "1", "a")
        v2 = self.app.create("usdc", 8453, "1", "b")
        self.pay(v1, tx(5))
        r = self.app.check(v2["id"], tx(5), "b")
        self.assertEqual(r["state"], "bad")
        self.assertRegex(r["why"], "underpaid|does not match")
        self.assertEqual(self.app.ledger.get(v2["id"])["status"], "open")

    def test_expired_and_bad_input(self):
        v = self.app.create("eth", 8453, "0.001", "a")
        self.assertIn("transaction hash", self.app.check(v["id"], "0x1234", "a")["why"])
        self.assertIn("unknown", self.app.check("a" * 26, tx(1), "a")["why"])
        self.now[0] += P.OPEN_S + 1
        self.pay(v, tx(7))
        self.assertIn("expired", self.app.check(v["id"], tx(7), "a")["why"])

    def test_rate_limits(self):
        for _ in range(20):
            self.app.create("usdc", 42161, "1", "same")
        with self.assertRaises(P.AmountError):
            self.app.create("usdc", 42161, "1", "same")
        self.app.create("usdc", 42161, "1", "someone-else")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.chains = {c: [FakeChain(c)] for c in P.CHAINS}
        cls.app = S.App(Path(cls.tmp.name), TO, rpcs=cls.chains, clock=lambda: T0)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(cls.app))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/donate"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def req(self, path, data=None, ctype="application/json"):
        r = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_index_and_headers(self):
        code, hdrs, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("default-src 'none'", hdrs["Content-Security-Policy"])
        self.assertNotIn("<script", body.decode())
        self.assertIn(TO, body.decode())
        self.assertIn("not affiliated", body.decode())

    def test_api_flow(self):
        code, _, body = self.req("/api/new", json.dumps({"asset": "eth", "chain_id": 1, "amount": "0.0003"}).encode())
        self.assertEqual(code, 200, body)
        s = json.loads(body)
        self.assertTrue(s["uri"].startswith(f"ethereum:{TO}@1?value="))
        self.assertEqual(s["to"], TO)
        c = self.chains[1][0]
        c.add_eth(tx(42), TO, int(s["units"]))
        code, _, body = self.req(f"/api/d/{s['id']}/check", json.dumps({"tx": tx(42)}).encode())
        r = json.loads(body)
        self.assertEqual(r["state"], "paid", r)
        self.assertEqual(r["donation"]["tx"], tx(42))
        code, _, page = self.req(f"/d/{s['id']}")
        self.assertIn("Thank you", page.decode())

    def test_api_rejects(self):
        code, _, body = self.req("/api/new", json.dumps({"asset": "usdc", "chain_id": 8453, "amount": "0.01"}).encode())
        self.assertEqual(code, 400)
        code, _, _ = self.req("/api/new", b"x" * (S.MAX_BODY + 1))
        self.assertEqual(code, 413)
        code, _, _ = self.req("/api/new", b"[1]")
        self.assertEqual(code, 400)
        code, _, _ = self.req("/../etc/passwd")
        self.assertEqual(code, 404)

    def test_form_flow_escapes_input(self):
        code, _, body = self.req("/new", b"what=usdc%3A8453&amount=%3Cscript%3E",
                                 "application/x-www-form-urlencoded")
        self.assertEqual(code, 400)
        self.assertNotIn("<script>", body.decode())
        self.assertIn("&lt;script&gt;", body.decode())
        r = urllib.request.Request(self.base + "/new", data=b"what=usdc%3A8453&amount=1.5",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(r) as resp:   # follows the 303 to the donation page
            page = resp.read().decode()
        self.assertIn("Send exactly", page)
        self.assertIn(P.USDC_TOKENS[8453], page)


if __name__ == "__main__":
    unittest.main()
