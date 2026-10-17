import unittest
from srnet import cli


class KeyNote(unittest.TestCase):
    def test_new_key_note_is_current(self):
        n = cli.NEW_KEY_NOTE
        self.assertNotIn("set your relayer once", n)  # a default relayer ships now
        self.assertIn("SRNet demo relayer", n)
        self.assertIn("scan the QR or send", n)
        self.assertIn("never need ETH", n)
        self.assertNotIn("A little ETH", n)
        self.assertIn("Back up the keystore file and your passphrase", n)
        self.assertNotIn("with ETH for gas and USDC", n)


if __name__ == "__main__":
    unittest.main()
