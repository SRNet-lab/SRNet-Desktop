"""Pure-Python chain layer: ABI encoding, signing, and the error paths. No network."""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# Never wait on a terminal: an unmocked prompt sees EOF and fails fast.
import io as _io
sys.stdin = _io.StringIO()
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

from srnet import chain, cli  # noqa: E402

# Well-known anvil/hardhat account 0. Vectors below were produced with Foundry `cast`.
KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
CONTRACT = "0x00000000000000000000000000000000000000c0"


class FakeRpc:
    """Answers JSON-RPC calls from a dict of method -> value or callable."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.calls.append(body)
        a = self.answers[body["method"]]
        res = a(body["params"]) if callable(a) else a
        if isinstance(res, Exception):
            raise res
        payload = res if isinstance(res, dict) and "error" in res else {"result": res}
        return io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": body["id"], **payload}).encode())


def word(n: int) -> str:
    return f"{n:064x}"


def ccfg(answers):
    fake = FakeRpc(answers)
    p = mock.patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: _ctx(fake(req, timeout)))
    p.start()
    return chain.ChainCfg(rpc="http://rpc.test", contract=CONTRACT, private_key=KEY), fake, p


class _ctx:
    def __init__(self, b):
        self.b = b

    def __enter__(self):
        return self.b

    def __exit__(self, *a):
        return False


class AbiAndSigning(unittest.TestCase):
    def test_selector_known(self):
        self.assertEqual(chain.selector("transfer(address,uint256)").hex(), "a9059cbb")

    def test_encode_start_lease_matches_cast(self):
        got = chain.encode_call("startLease(bytes32,uint256,uint256)", "0x" + "bb" * 32, 1_000_000, 1).hex()
        self.assertEqual(got, "427bb23a" + "bb" * 32 + word(1_000_000) + word(1))

    def test_decode_tuple(self):
        raw = bytes.fromhex(word(5) + word(7))
        self.assertEqual(chain.decode_result(["uint256", "uint256"], raw), (5, 7))

    def test_personal_sign_matches_cast(self):
        from eth_account import Account
        c = chain.ChainCfg.__new__(chain.ChainCfg)
        c._acct = Account.from_key(KEY)
        self.assertEqual(c.address(), ADDR)
        self.assertEqual(c.sign_claim("hello srnet"),
                         "0x889a82ad9d0cafcca82ef531bb2864ed34e5c9338171aa20637fb42cc1cb790c"
                         "0fec37e224557fd491d06a2fd3d139f0d1d57af66b0f2b779c2f14dcb193cca51c")

    def test_eip1559_tx_matches_cast_mktx(self):
        from eth_account import Account
        tx = chain.build_tx(421614, 7, CONTRACT, "0x" + chain.encode_call("fund(uint256)", 2_000_000).hex(),
                            100_000, 10**9, 3 * 10**9)
        self.assertEqual(chain.sign_tx(Account.from_key(KEY), tx),
                         "0x02f89283066eee07843b9aca0084b2d05e00830186a09400000000000000000000000000000000000000c0"
                         "80a4ca1d209d00000000000000000000000000000000000000000000000000000000001e8480c001a0afc54bae"
                         "77b00834aa06c50a6ddef04a6c258369622cdef58a939594d4d4dee4a038342848be45339dc512799f8d7642a1"
                         "565909f0f7a56ddc85e82d8743a3cd20")

    def test_explain_custom_error(self):
        data = "0x" + chain.selector("InsufficientAvailable()").hex()
        self.assertIn("top up first", chain.explain_revert(data))
        self.assertIsNone(chain.explain_revert("0x"))


class ErrorPaths(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def test_missing_rpc(self):
        with self.assertRaises(chain.ChainError) as e:
            chain.ChainCfg(rpc="", contract=CONTRACT, private_key=KEY)
        self.assertIn("SRNET_RPC", str(e.exception))

    def test_bad_key(self):
        with self.assertRaises(chain.ChainError) as e:
            chain.ChainCfg(rpc="http://x", contract=CONTRACT, private_key="0x1234")
        self.assertIn("private key", str(e.exception))

    def test_unreachable_rpc(self):
        mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")).start()
        c = chain.ChainCfg(rpc="http://rpc.test", contract=CONTRACT, private_key=KEY)
        with self.assertRaises(chain.ChainError) as e:
            c.chainid()
        self.assertIn("can't reach RPC", str(e.exception))

    def test_unsupported_chain_refused(self):
        c, _, _ = ccfg({"eth_chainId": "0x1"})
        with self.assertRaises(SystemExit):
            c.chainid()

    def test_arbitrum_one_allowed(self):
        c, _, _ = ccfg({"eth_chainId": hex(42161)})
        self.assertEqual(c.chainid(), 42161)

    def test_insufficient_usdc(self):
        def eth_call(p):
            sel = p[0]["data"][2:10]
            if sel == chain.selector("token()").hex():
                return "0x" + "0" * 24 + "0d" * 20
            return "0x" + word(500_000)  # 0.50 USDC balance
        c, _, _ = ccfg({"eth_chainId": "0x7a69", "eth_call": eth_call})
        with self.assertRaises(chain.ChainError) as e:
            c.fund(2_000_000)
        self.assertIn("not enough USDC", str(e.exception))

    def test_no_allowance_triggers_approve(self):
        sent = []
        def eth_call(p):
            sel = p[0]["data"][2:10]
            if sel == chain.selector("token()").hex():
                return "0x" + "0" * 24 + "0d" * 20
            if sel == chain.selector("allowance(address,address)").hex():
                return "0x" + word(0)
            return "0x" + word(10_000_000)
        def est(p):
            sent.append(p[0]["data"][2:10])
            return hex(50_000)
        c, _, _ = ccfg({"eth_chainId": "0x7a69", "eth_call": eth_call, "eth_estimateGas": est,
                        "eth_getBlockByNumber": {"baseFeePerGas": hex(10**9), "timestamp": "0x1"},
                        "eth_maxPriorityFeePerGas": hex(10**9), "eth_getTransactionCount": "0x0",
                        "eth_getBalance": hex(10**18), "eth_sendRawTransaction": "0x" + "ab" * 32,
                        "eth_getTransactionReceipt": {"status": "0x1", "logs": []}})
        c.fund(2_000_000)
        self.assertEqual(sent, [chain.selector("approve(address,uint256)").hex(), chain.selector("fund(uint256)").hex()])

    def test_insufficient_deposit_for_lease(self):
        c, _, _ = ccfg({"eth_chainId": "0x7a69", "eth_call": "0x" + word(100)})
        with self.assertRaises(chain.ChainError) as e:
            c.start_lease("0x" + "bb" * 32, 1_000_000, 1)
        self.assertIn("top up", str(e.exception))

    def test_revert_decoded_from_rpc(self):
        rev = {"error": {"code": 3, "message": "execution reverted",
                         "data": "0x" + chain.selector("RenewTooEarly()").hex()}}
        c, _, _ = ccfg({"eth_chainId": "0x7a69", "eth_estimateGas": rev})
        with self.assertRaises(chain.ChainError) as e:
            c.renew(1)
        self.assertIn("too early to renew", str(e.exception))

    def test_no_gas(self):
        c, _, _ = ccfg({"eth_chainId": "0x7a69", "eth_estimateGas": hex(50_000),
                        "eth_getBlockByNumber": {"baseFeePerGas": hex(10**9)},
                        "eth_maxPriorityFeePerGas": hex(10**9), "eth_getTransactionCount": "0x0",
                        "eth_getBalance": "0x0"})
        with self.assertRaises(chain.ChainError) as e:
            c.cash_out(1)
        self.assertIn("ETH for gas", str(e.exception))

    def test_cli_wrong_chain_message(self):
        import test_cli_unit as T
        doc, sesh, d = T._listing_doc()
        board = d / "board.json"
        board.write_text(json.dumps(doc))
        home = Path(tempfile.mkdtemp())
        (home / "visibility.ack").write_text("I UNDERSTAND")
        ccfg({"eth_chainId": hex(421614)})
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch.object(cli.warning, "ensure_ack", lambda root: None):
            rc = cli.main(["--home", str(home), "buy", sesh, "--listings", str(board), "--client-sesh", "x.sesh",
                           "--rpc", "http://rpc.test", "--private-key", KEY])
        self.assertEqual(rc, 1)
        self.assertIn("wrong chain", err.getvalue())

    def test_cli_refuses_unknown_contract_on_arbitrum_one(self):
        import listing as L
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        k = Ed25519PrivateKey.generate()
        seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                               serialization.NoEncryption())
        pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        d = Path(tempfile.mkdtemp())
        (d / "exit.key").write_text((seed + pub).hex())
        doc = L.make(L.load_identity(d / "exit.key"), price_per_hour_usdc="1.00", max_hours=168,
                     payout="0x" + "aa" * 20, vendor_id="0x" + "bb" * 32, chainid=42161,
                     contract="0x" + "c0" * 20, region="test", policy=["tcp/443"], dns="10.195.0.53")
        sesh = L.sesh_of(pub)
        board = d / "board.json"
        board.write_text(json.dumps(doc))
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch.object(cli.warning, "ensure_ack", lambda root: None):
            rc = cli.main(["--home", tempfile.mkdtemp(), "buy", sesh, "--listings", str(board),
                           "--client-sesh", "x.sesh", "--mock"])
        self.assertEqual(rc, 1)
        self.assertIn("not the SRNetBalance deployment", err.getvalue())

    def test_cli_missing_config_message(self):
        import test_cli_unit as T
        doc, sesh, d = T._listing_doc()
        board = d / "board.json"
        board.write_text(json.dumps(doc))
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch.object(cli.warning, "ensure_ack", lambda root: None), \
                mock.patch.dict("os.environ", {}, clear=True):
            rc = cli.main(["--home", tempfile.mkdtemp(), "buy", sesh, "--listings", str(board),
                           "--client-sesh", "x.sesh"])
        self.assertEqual(rc, 2)
        self.assertIn("srnet doctor", err.getvalue())


class RenewChain(unittest.TestCase):
    """The contract lets each lease be renewed once (AlreadyRenewed). The client must always renew the
    newest lease and persist it."""

    def _state(self, ch, lease_id):
        from srnet import state
        d = Path(tempfile.mkdtemp())
        s = state.State(d)
        s.data["lease"] = {"seller": "x", "contract": ch.contract, "chainid": 31337, "current": lease_id,
                           "renewed_from": [], "auto_renew": True, "client_sesh": "c.sesh",
                           "spool": str(d / "spool"), "rate_per_hour": 500_000}
        s.save()
        return s, d

    def test_renews_newest_each_hour_and_persists(self):
        from srnet import state
        ch = chain.MockChain()
        a = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        s, d = self._state(ch, a)
        ids = [a]
        for _ in range(4):
            ch.clock = ch.holds[ids[-1]]["end"] - 300
            s = state.State(d)  # fresh process each hour: state must come from disk
            r = cli.renew_once(ch, s)
            self.assertTrue(r["renewed"], r)
            self.assertEqual(r["prev"], ids[-1])
            ids.append(r["lease_id"])
            self.assertEqual(json.loads((d / "state.json").read_text())["lease"]["current"], ids[-1])
        self.assertEqual(json.loads((d / "state.json").read_text())["lease"]["renewed_from"], ids[:-1])

    def test_catches_up_when_local_state_is_stale(self):
        ch = chain.MockChain()
        a = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        s, d = self._state(ch, a)
        ch.clock = ch.holds[a]["end"] - 300
        b = ch.renew(a, 1)  # renewed, but the result never reached state.json
        ch.clock = ch.holds[b]["end"] - 300
        r = cli.renew_once(ch, s)
        self.assertTrue(r["renewed"], r)
        self.assertEqual(r["prev"], b)
        self.assertEqual(s.data["lease"]["renewed_from"], [a, b])
        self.assertTrue((d / "spool" / f"hold-{b}.json").exists())

    def test_second_renewal_of_same_lease_refused(self):
        ch = chain.MockChain()
        a = ch.start_lease("0x" + "01" * 32, 500_000, 1)
        ch.clock = ch.holds[a]["end"] - 300
        ch.renew(a, 1)
        with self.assertRaises(RuntimeError):
            ch.renew(a, 1)

    def test_already_renewed_revert_is_readable(self):
        self.assertIn("newest lease", chain.explain_revert("0x" + chain.selector("AlreadyRenewed()").hex()))


if __name__ == "__main__":
    unittest.main()
