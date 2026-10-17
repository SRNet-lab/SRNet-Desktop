"""Claim delivery client <-> seller receiver + watcher (loopback, mocked chain)."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
if sys.platform == "win32":  # the seller side (admission store) is Linux-only
    raise unittest.SkipTest("seller receiver/watcher run on Linux")
import admission as A  # noqa: E402
import claim_receiver as R  # noqa: E402
import watcher as W  # noqa: E402
from srnet import deliver as D  # noqa: E402

SELLER = "yryonyebyryonyebyryonyebyryonyebyryonyebyryonyebyryo.sesh"
CLIENT = "ycbogyadycbogyadycbogyadycbogyadycbogyadycbogyadycbo.sesh"
OURS, USER = "0x" + "aa" * 20, "0x" + "11" * 20
SIG = "0x" + "ab" * 65


class V(W.MockVerifier):
    def recover_ok(self, address, message, sig):
        return sig == SIG and address.lower() == USER


class Deliver(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        A.keygen(d / "k")
        now = int(time.time())
        ch = W.MockChain(clock=now)
        ch.holds[9] = W.Hold(USER, 100, OURS, now + 3600, True, rate_per_hour=100)
        self.w = W.Watcher(ch, V(), payout_wallet=OURS, seller_sesh=SELLER, issuer_key=str(d / "k"),
                           store=str(d / "adm.json"), state=str(d / "st.json"), min_amount=100)
        self.spool, wake = d / "spool", threading.Event()
        self.srv = R.serve(R.Receiver(self.spool, wake), "127.0.0.1", 0)
        self.stop = False

        def loop():  # the seller's watcher loop, woken by deliveries
            while not self.stop:
                if wake.wait(0.2):
                    wake.clear()
                    self.w.poll_spool(self.spool)
        threading.Thread(target=loop, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1/claim"
        self.claim = {"v": 1, "hold_id": 9, "client_sesh": CLIENT, "sig": SIG}
        self.listing = {"seller": SELLER, "claim_port": self.srv.server_address[1]}

    def tearDown(self):
        self.stop = True
        self.srv.shutdown()

    def test_url_from_signed_listing(self):
        self.assertEqual(D.claim_url({"seller": SELLER}), f"http://{SELLER}:8650/v1/claim")
        
        self.assertEqual(D.claim_url(self.listing), f"http://{SELLER}:{self.listing['claim_port']}/v1/claim")
        # Prefer signed claim_url from seller pack when present.
        cu = "http://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.sesh/claim"
        self.assertEqual(D.claim_url({"seller": SELLER, "claim_url": cu}), cu)
        self.assertEqual(
            D.claim_url({"seller": SELLER, "claim_url": cu.replace("/claim", "/v1/claim")}),
            cu.replace("/claim", "/v1/claim"),
        )
        with self.assertRaises(ValueError):
            D.claim_url({"seller": SELLER, "claim_url": "http://evil.example/claim"})
        with self.assertRaises(ValueError):
            D.claim_url({"seller": "evil.example.com"})

    def test_two_node_delivery_admits(self):
        r = D.deliver(self.claim, self.listing, url=self.url, poll=0.1, wait_admit=5, sleep=time.sleep)
        self.assertEqual((r["delivered"], r["status"]), (True, "admitted"), r)
        again = D.deliver(self.claim, self.listing, url=self.url, wait_admit=0)
        self.assertEqual(again["status"], "admitted")  # idempotent

    def test_retries_then_succeeds(self):
        calls = []

        def flaky(url, body):
            calls.append(url)
            if len(calls) < 3:
                raise OSError("router not ready")
            return D.http_transport(url, body)
        r = D.deliver(self.claim, self.listing, url=self.url, transport=flaky, sleep=lambda s: None,
                      wait_admit=2, poll=0.1)
        self.assertTrue(r["delivered"])
        self.assertGreaterEqual(len(calls), 3)

    def test_gives_up_cleanly(self):
        def dead(url, body):
            raise OSError("no route")
        r = D.deliver(self.claim, self.listing, url=self.url, transport=dead, attempts=3, sleep=lambda s: None)
        self.assertEqual((r["delivered"], r["status"]), (False, "undelivered"))

    def test_refuses_non_sesh_host(self):
        r = D.deliver(self.claim, self.listing, url="http://example.com:8650/v1/claim", sleep=lambda s: None)
        self.assertFalse(r["delivered"])

    def test_plain_text_202_from_older_seller_pack_counts_as_received(self):
        r = D.deliver(self.claim, self.listing, url=self.url, transport=lambda u, b: (202, {}),
                      sleep=lambda s: None)
        self.assertEqual((r["delivered"], r["status"]), (True, "received"))

    def test_conflict_reported(self):
        D.deliver(self.claim, self.listing, url=self.url, wait_admit=0)
        r = D.deliver(dict(self.claim, sig="0x" + "cd" * 65), self.listing, url=self.url, wait_admit=0)
        self.assertEqual(r["status"], "conflict")


if __name__ == "__main__":
    unittest.main()
