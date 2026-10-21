"""Static checks on the trusted shell copy (no browser)."""
import unittest
from pathlib import Path
SHELL = Path(__file__).resolve().parents[1] / "typed" / "shell"

class ShellCopy(unittest.TestCase):
    def test_host_hidden_label_and_us_spelling(self):
        html = (SHELL / "shell.html").read_text()
        js = (SHELL / "shell.js").read_text()
        self.assertIn(">Host-hidden<", html)
        self.assertIn("honors host hides", html)
        self.assertNotIn("honours", html)
        self.assertIn("honors host hides", js)
        self.assertNotIn("honours", js)
        self.assertIn("Honor host hides", js)

    def test_no_wallet_in_core(self):
        html = (SHELL / "shell.html").read_text()
        js = (SHELL / "shell.js").read_text()
        for word in ("Wallet", "passphrase", "relayer", "SRNet balance", "wallet/"):
            self.assertNotIn(word, html)
            self.assertNotIn(word, js)
        self.assertIn('<nav id="ribbon"><button data-b="home">Home</button></nav>', html)
        self.assertIn('<div id="addons"></div>', html)

    def test_addon_tabs_and_pay_sheet(self):
        js = (SHELL / "shell.js").read_text()
        self.assertIn("function addonTabs(list)", js)
        self.assertIn('"/addon/" + a.name + "/tab.js"', js)
        self.assertIn('api("preflight", {})', js)
        self.assertIn('"send exactly: "', js)
        self.assertIn('"Copy address"', js)
        self.assertIn('api("pay/order"', js)
        self.assertNotIn("pay/confirm", js)

    def test_home_shows_the_demo_host(self):
        import json, re
        html = (SHELL / "shell.html").read_text()
        cfg = json.loads((SHELL.parent / "default-client.json").read_text())
        host = re.match(r"http://([a-z0-9]+\.sesh)", cfg["mail"]["hosts"][0]).group(1)
        self.assertIn(f'Demo host: <code id="demo-host" title="Click to put it in the box">{host}</code>', html)
        self.assertIn('<button id="join-go">Look up</button>', html)

if __name__ == "__main__":
    unittest.main()
