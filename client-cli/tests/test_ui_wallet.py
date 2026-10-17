"""Wallet / Top up and SNApps panes of the local UI."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

from srnet import chain, gasless, keys, state, ui  # noqa: E402

ADDR = "0x" + "ab" * 20


class WalletUiTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.be = ui.Backend(state.State(self.home))
        self.srv, url = ui.serve(self.be, legacy=True)
        self.token = url.split("k=", 1)[1]
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-CSRF-Token": self.token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_defaults(self):
        self.assertEqual(chain.DEFAULT_CONTRACT, "0x7E0d01B7dd916BdAB96B0971ab92D160E1664431")
        self.assertEqual(gasless.DEFAULT_RELAY_FEE, 50_000)
        self.assertEqual(self.be.relayer_url(), chain.DEFAULT_RELAYER)  # the demo relayer, over .sesh
        self.assertTrue(chain.DEFAULT_RELAYER.startswith("http://") and ".sesh/" in chain.DEFAULT_RELAYER)
        self.assertEqual(chain.relayer_url({"contract": "0x" + "11" * 20}), "")  # never for another contract
        self.assertEqual(chain.relayer_url({"relayer": "off"}), "")

    def test_topup_info_and_qr(self):
        with mock.patch.object(keys, "address_of", return_value=ADDR):
            code, r = self.post("/api/topup/info", {"amount": 10})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["ok"])
        self.assertEqual(r["chainid"], 42161)
        self.assertIn("transfer?address=", r["uri"])
        self.assertIn("uint256=10000000", r["uri"])
        self.assertIn("Send only USDC", r["warning"])
        self.assertEqual(r["fee_pct"], 3)
        self.assertAlmostEqual(r["relay_fee"], 0.05)
        if r["qr"]:
            with urllib.request.urlopen(f"{self.base}/qr.svg?k={self.token}") as resp:
                self.assertIn(b"<svg", resp.read())
        try:
            urllib.request.urlopen(f"{self.base}/qr.svg?k=wrong")
            self.fail("qr without token")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_topup_needs_relayer_and_passphrase(self):
        self.assertTrue(self.post("/api/relayer", {"url": "off"})[1]["ok"])
        code, r = self.post("/api/topup/start", {"amount": 5})
        self.assertEqual(code, 400)
        code, r = self.post("/api/topup/start", {"amount": 5, "passphrase": "x"})
        self.assertFalse(r["ok"])
        self.assertIn("relayer", r["err"])
        code, r = self.post("/api/topup/start", {"amount": 0, "passphrase": "x"})
        self.assertEqual(code, 400)

    def test_relayer_url_rules(self):
        self.assertFalse(self.post("/api/relayer", {"url": "http://evil.example/"})[1]["ok"])
        self.assertFalse(self.post("/api/relayer", {"url": "file:///etc/passwd"})[1]["ok"])
        self.assertTrue(self.post("/api/relayer", {"url": "http://127.0.0.1:8645"})[1]["ok"])
        self.assertEqual(self.be.relayer_url(), "http://127.0.0.1:8645")
        self.assertTrue(self.post("/api/relayer", {"url": ""})[1]["ok"])
        self.assertEqual(self.be.relayer_url(), chain.DEFAULT_RELAYER)  # cleared: back to the default
        self.assertTrue(self.post("/api/relayer", {"url": "off"})[1]["ok"])
        self.assertEqual(self.be.relayer_url(), "")

    def test_snapps_link_is_loopback(self):
        self.assertTrue(self.post("/api/snapps", {})[1]["url"].startswith("http://127.0.0.1:"))

    def test_page_has_three_tabs(self):
        with urllib.request.urlopen(f"{self.base}/?k={self.token}") as r:
            page = r.read().decode()
        for t in ("SNApps", "Exits", "Wallet / Top up"):
            self.assertIn(t, page)
        self.assertIn("I've sent USDC — deposit now", page)
        self.assertIn("Unlock to see balances", page)
        self.assertNotIn("Deposit when it arrives", page)


if __name__ == "__main__":
    unittest.main()
