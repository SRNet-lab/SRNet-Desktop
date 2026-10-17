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

    def test_relayer_gate_and_empty_slots(self):
        html = (SHELL / "shell.html").read_text()
        js = (SHELL / "shell.js").read_text()
        self.assertIn("w-relayerhint", html)
        self.assertIn("id=\"w-topup\" disabled", html)
        self.assertIn("id=\"w-topupstate\" hidden", html)
        self.assertIn("id=\"w-uri\" hidden", html)
        self.assertIn("savedRelayer", js)
        self.assertIn("Save a relayer URL first", js)

    def test_board_timeout_abort(self):
        js = (SHELL / "shell.js").read_text()
        self.assertIn("AbortController", js)
        self.assertIn("Couldn't load the exit board (timed out). Try Refresh.", js)
        self.assertIn("BOARD_DEADLINE_MS = 20000", js)
        self.assertIn("Promise.race", js)
        self.assertNotIn("board unavailable", js)
        self.assertIn("x-cs-hint", (SHELL / "shell.html").read_text())

if __name__ == "__main__":
    unittest.main()
