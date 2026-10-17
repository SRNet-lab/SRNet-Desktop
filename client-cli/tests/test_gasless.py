"""Gasless top-up: EIP-681 URI, permit + intent signing, call encoding, relayer guards. No network."""
import json
import sys
import urllib.error
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

from eth_account import Account  # noqa: E402
from eth_account.messages import encode_typed_data  # noqa: E402

from srnet import chain, gasless, relayer  # noqa: E402

KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ACCT = Account.from_key(KEY)
C = "0x00000000000000000000000000000000000000c0"
U = "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d"


def req(**kw):
    base = dict(chainid=421614, contract=C, usdc=U, amount=5_000_000, permit_nonce=0, deadline=4_000_000_000, nonce=7)
    base.update(kw)
    return gasless.sign_topup(ACCT, **base)


class TestUri(unittest.TestCase):
    def test_eip681(self):
        u = gasless.eip681_uri(U, ACCT.address, 42161, 5_000_000)
        self.assertEqual(u, f"ethereum:{U}@42161/transfer?address={ACCT.address}&uint256=5000000")

    def test_screen_warns_own_wallet(self):
        s = gasless.topup_screen(ACCT.address, 421614, U)
        self.assertIn("funds YOUR SRNet wallet", s)
        self.assertIn(ACCT.address, s)
        self.assertIn("do not need ETH", s)


class TestSigning(unittest.TestCase):
    def test_intent_recovers_owner(self):
        r = req()
        msg = encode_typed_data({"name": "SRNetBalance", "version": "1", "chainId": 421614,
                                 "verifyingContract": C}, gasless.FUND_TYPES, r["intent"])
        self.assertEqual(Account.recover_message(msg, signature=r["sig"]), ACCT.address)

    def test_permit_recovers_owner(self):
        r = req()
        p = r["permit"]
        msg = encode_typed_data({"name": "USD Coin", "version": "2", "chainId": 421614, "verifyingContract": U},
                                gasless.PERMIT_TYPES,
                                {"owner": ACCT.address, "spender": C, "value": 5_000_000, "nonce": 0,
                                 "deadline": 4_000_000_000})
        self.assertEqual(Account.recover_message(msg, vrs=(p["v"], int(p["r"], 16), int(p["s"], 16))), ACCT.address)

    def test_defaults_beneficiary_is_self_and_any_relayer(self):
        i = req()["intent"]
        self.assertEqual(i["beneficiary"], ACCT.address)
        self.assertEqual(i["relayer"], gasless.ZERO)
        self.assertEqual(i["relayFee"], gasless.DEFAULT_RELAY_FEE)

    def test_encode_selector(self):
        data = gasless.encode_fund_call(req())
        self.assertTrue(data.startswith("0x" + chain.selector(gasless.FUND_SIG).hex()))

    def test_wait_for_usdc(self):
        seq = iter([0, 0, 6_000_000])
        self.assertEqual(gasless.wait_for_usdc(lambda: next(seq), 5_000_000, sleep=lambda s: None), 6_000_000)
        with self.assertRaises(chain.ChainError):
            gasless.wait_for_usdc(lambda: 0, 1, timeout=0, sleep=lambda s: None)


class FakeChain:
    contract = C

    def __init__(self):
        self.sent = []

    def chainid(self):
        return 421614

    def address(self):
        return "0x" + "ab" * 20

    def send_data(self, data, max_gas=None):
        self.sent.append((data, max_gas))
        return {"transactionHash": "0x01", "gasUsed": hex(250_000), "effectiveGasPrice": hex(10**7)}


class TestRelayer(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.ch = FakeChain()
        self.r = relayer.Relayer(self.ch, min_fee=50_000, gas_budget_wei=10**13, ip_limit=(2, 60),
                                 owner_limit=(100, 60), clock=lambda: self.t[0])

    def test_ok(self):
        code, out = self.r.handle(req(), "1.1.1.1")
        self.assertEqual(code, 200)
        self.assertEqual(out["gas_wei"], 250_000 * 10**7)

    def test_rejects_low_fee_wrong_contract_chain_pinned(self):
        self.assertEqual(self.r.handle(req(relay_fee=1))[0], 400)
        self.assertEqual(self.r.handle(req(contract="0x" + "11" * 20))[0], 400)
        self.assertEqual(self.r.handle(req(chainid=42161))[0], 400)
        self.assertEqual(self.r.handle(req(relayer="0x" + "22" * 20))[0], 400)
        self.assertEqual(self.r.handle({"intent": {}})[0], 400)
        self.assertEqual(self.ch.sent, [])

    def test_rate_limit(self):
        self.assertEqual(self.r.handle(req(), "ip")[0], 200)
        self.assertEqual(self.r.handle(req(nonce=8), "ip")[0], 200)
        self.assertEqual(self.r.handle(req(nonce=9), "ip")[0], 429)
        self.t[0] += 61
        self.assertEqual(self.r.handle(req(nonce=9), "ip")[0], 200)

    def test_gas_budget(self):
        r = relayer.Relayer(self.ch, gas_budget_wei=250_000 * 10**7, clock=lambda: self.t[0])
        self.assertEqual(r.handle(req())[0], 200)
        self.assertEqual(r.handle(req(nonce=8))[0], 503)
        self.t[0] += 86401
        self.assertEqual(r.handle(req(nonce=8))[0], 200)

    def test_revert_not_sent(self):
        def boom(data, max_gas=None):
            raise chain.ChainError("BadSignature")
        self.ch.send_data = boom
        self.assertEqual(self.r.handle(req())[0], 422)


class TestRelayerHTTP(unittest.TestCase):
    """The relayer behind a path router on a .sesh name: /relayer/<kind>, X-Forwarded-For from loopback."""

    def _srv(self, trust):
        import urllib.request
        t = [1000.0]
        r = relayer.Relayer(FakeChain(), ip_limit=(2, 60), owner_limit=(100, 60), clock=lambda: t[0])
        srv = relayer.serve(r, "127.0.0.1", 0, trust_proxy=trust)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def post(path, body, fwd=None):
            h = {"Content-Type": "application/json"}
            if fwd:
                h["X-Forwarded-For"] = fwd
            q = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=h)
            try:
                with urllib.request.urlopen(q, timeout=10) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code
        return base, post

    def test_prefix_health_and_forwarded_ip(self):
        import urllib.request
        base, post = self._srv(trust=True)
        with urllib.request.urlopen(base + "/relayer/", timeout=10) as resp:
            info = json.loads(resp.read())
        self.assertEqual((info["ok"], info["relayer"], info["chainid"]), (True, "0x" + "ab" * 20, 421614))
        self.assertEqual(gasless.relayer_info(base + "/relayer")["contract"], C)
        self.assertEqual(post("/relayer/fund", req(), "fd2e::a"), 200)
        self.assertEqual(post("/relayer/fund", req(nonce=8), "fd2e::a"), 200)
        self.assertEqual(post("/relayer/fund", req(nonce=9), "fd2e::a"), 429)  # same .sesh peer: rate limited
        self.assertEqual(post("/relayer/fund", req(nonce=9), "fd2e::b"), 200)  # another one is not
        self.assertEqual(post("/relayer/nope", req()), 404)

    def test_forwarded_ignored_without_trust_proxy(self):
        _, post = self._srv(trust=False)
        self.assertEqual(post("/fund", req(), "fd2e::a"), 200)
        self.assertEqual(post("/fund", req(nonce=8), "fd2e::b"), 200)
        self.assertEqual(post("/fund", req(nonce=9), "fd2e::c"), 429)  # all one loopback peer


if __name__ == "__main__":
    unittest.main()
