"""Encrypted keystore: create, import, unlock, wrong passphrase, key source resolution."""
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# Never wait on a terminal: an unmocked prompt sees EOF and fails fast.
import io as _io
sys.stdin = _io.StringIO()
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]

from srnet import keys, cli  # noqa: E402

KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
FAST = 2 ** 10  # scrypt cost for tests only


class Keystore(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.ks = self.d / "keystore.json"

    def test_import_unlock_roundtrip(self):
        self.assertEqual(keys.write(self.ks, KEY, "pw", iterations=FAST), ADDR)
        self.assertEqual(keys.unlock(self.ks, "pw"), KEY)
        if sys.platform != "win32":  # Windows: protected by the user-profile ACL, not mode bits
            self.assertEqual(stat.S_IMODE(os.stat(self.ks).st_mode), 0o600)
        self.assertEqual(keys.address_of(self.ks).lower(), ADDR.lower())
        self.assertNotIn(KEY[2:], self.ks.read_text())

    def test_v3_scrypt_format(self):
        import json
        keys.write(self.ks, KEY, "pw", iterations=FAST)
        d = json.loads(self.ks.read_text())
        self.assertEqual(d["version"], 3)
        self.assertEqual(d["crypto"]["kdf"], "scrypt")

    def test_new(self):
        addr = keys.new(self.ks, "pw", iterations=FAST)
        from eth_account import Account
        self.assertEqual(Account.from_key(keys.unlock(self.ks, "pw")).address, addr)

    def test_wrong_passphrase(self):
        keys.write(self.ks, KEY, "pw", iterations=FAST)
        with self.assertRaises(keys.KeyError_) as e:
            keys.unlock(self.ks, "nope")
        self.assertIn("wrong keystore passphrase", str(e.exception))

    def test_missing_keystore(self):
        with self.assertRaises(keys.KeyError_) as e:
            keys.unlock(self.ks, "pw")
        self.assertIn("srnet key new", str(e.exception))

    def test_refuses_overwrite(self):
        keys.write(self.ks, KEY, "pw", iterations=FAST)
        with self.assertRaises(keys.KeyError_):
            keys.write(self.ks, KEY, "pw", iterations=FAST)

    def test_resolve_prefers_keystore_with_env_password(self):
        keys.write(self.ks, KEY, "pw", iterations=FAST)
        args = SimpleNamespace(private_key=None, keystore=None)
        with mock.patch.dict(os.environ, {"SRNET_KEYSTORE_PASSWORD": "pw"}, clear=True):
            pk, src = keys.resolve(args, {}, self.d)
        self.assertEqual(pk, KEY)
        self.assertIn("keystore", src)

    def test_resolve_plaintext_warns(self):
        args = SimpleNamespace(private_key=None, keystore=None)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"SRNET_PRIVATE_KEY": KEY}, clear=True), mock.patch("sys.stderr", err):
            pk, src = keys.resolve(args, {}, self.d)
        self.assertEqual(pk, KEY)
        self.assertIn("plaintext", src)
        self.assertIn("warning", err.getvalue())

    def test_locked_without_tty_or_env(self):
        keys.write(self.ks, KEY, "pw", iterations=FAST)
        args = SimpleNamespace(private_key=None, keystore=None)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stdin", io.StringIO()):
            with self.assertRaises(keys.KeyError_) as e:
                keys.resolve(args, {}, self.d)
        self.assertIn("SRNET_KEYSTORE_PASSWORD", str(e.exception))

    def test_cli_key_import_and_doctor_source(self):
        env = {"SRNET_PRIVATE_KEY": KEY, "SRNET_KEYSTORE_PASSWORD": "pw"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("sys.stdout", out):
            rc = cli.main(["--home", str(self.d), "key", "import", "--from-env"])
        self.assertEqual(rc, 0)
        self.assertIn(ADDR, out.getvalue())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"SRNET_KEYSTORE_PASSWORD": "pw"}, clear=True), \
                mock.patch("sys.stdout", out):
            cli.main(["--home", str(self.d), "doctor"])
        self.assertIn("key source: keystore", out.getvalue())


if __name__ == "__main__":
    unittest.main()
