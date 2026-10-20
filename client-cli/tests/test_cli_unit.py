"""The SRNet core client: no wallet or eth libraries, add-on loading, and the install hint when
the exits add-on is missing."""
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.stdin = io.StringIO()
sys.path[:0] = [str(ROOT)]

from srnet import addons, cli  # noqa: E402


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err), \
            mock.patch.dict(os.environ, env or {"SRNET_NO_ADDONS": "1"}):
        addons.load(refresh=True)
        try:
            rc = cli.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


class NoEth(unittest.TestCase):
    def test_core_imports_without_eth_libraries(self):
        """Run in a fresh interpreter where importing any eth library fails."""
        code = ("import sys, builtins\n"
                "real = builtins.__import__\n"
                "def imp(name, *a, **k):\n"
                "    if name.split('.')[0] in ('eth_account', 'eth_abi', 'eth_keys', 'eth_utils', 'web3'):\n"
                "        raise ImportError('blocked ' + name)\n"
                "    return real(name, *a, **k)\n"
                "builtins.__import__ = imp\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "import srnet.cli, srnet.addons, srnet.state, srnet.rpc, srnet.preflight\n"
                "print('ok')\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, SRNET_NO_ADDONS="1"))
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    def test_no_wallet_or_exit_modules_left(self):
        names = {p.stem for p in (ROOT / "srnet").glob("*.py")}
        for gone in ("chain", "payments", "gasless", "relayer", "keys", "deliver", "board", "listings",
                     "backend", "killswitch", "tunneldns", "dnsproxy", "warning", "ui", "priv"):
            self.assertNotIn(gone, names)
        self.assertFalse((ROOT / "vendor").exists())
        text = (ROOT / "pyproject.toml").read_text()
        for dep in ("eth-account", "eth-abi", "web3", "vendor"):
            self.assertNotIn(dep, text)


class Hint(unittest.TestCase):
    def test_exit_commands_print_the_install_hint(self):
        for cmd in addons.EXITS_COMMANDS:
            rc, out, err = run(["--home", tempfile.mkdtemp(), cmd])
            self.assertEqual(rc, 2, cmd)
            self.assertIn("Exits are an add-on", err)
            self.assertIn("pip install", err)

    def test_help_lists_core_commands_only(self):
        rc, out, err = run(["--help"])
        self.assertEqual(rc, 0)
        for c in ("doctor", "snapps", "ui", "open", "mail"):
            self.assertIn(c, out)
        for gone in ("wallet", "balance", "relayer", "renew"):
            self.assertNotIn(gone, out.lower())


class FakeAddon:
    name, title, version = "exits", "Exits", "9.9"

    def __init__(self):
        self.ran = []

    def register(self, sub):
        p = sub.add_parser("exits", help="list exits")
        p.add_argument("--x", default="1")
        return {"exits": lambda args, st: self.ran.append(args.x) or 0}

    def doctor(self, st, line):
        line(True, "fake add-on check", "fine")

    def ui_manifest(self):
        return {"name": "exits", "title": "Exits", "tab": True, "slow": ["connect"]}

    def start_ui_backend(self, st):
        return "http://127.0.0.1:1/#tok"


FAKE = FakeAddon()
sys.modules["fake_srnet_addon"] = types.SimpleNamespace(ADDON=FAKE)


class Loading(unittest.TestCase):
    ENV = {"SRNET_ADDON_MODULES": "fake_srnet_addon:ADDON"}

    def test_addon_commands_run(self):
        FAKE.ran.clear()
        rc, out, err = run(["--home", tempfile.mkdtemp(), "exits", "--x", "7"], self.ENV)
        self.assertEqual(rc, 0, err)
        self.assertEqual(FAKE.ran, ["7"])
        rc, out, err = run(["--home", tempfile.mkdtemp(), "buy"], self.ENV)
        self.assertEqual(rc, 2)  # the add-on did not register buy: still the hint
        self.assertIn("Exits are an add-on", err)

    def test_disabled_and_broken_addons(self):
        with mock.patch.dict(os.environ, {"SRNET_NO_ADDONS": "1", **self.ENV}):
            self.assertEqual(addons.load(refresh=True), {})
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"SRNET_ADDON_MODULES": "no_such_module:ADDON"}), \
                mock.patch("sys.stderr", err):
            os.environ.pop("SRNET_NO_ADDONS", None)
            self.assertNotIn("no_such_module", addons.load(refresh=True))
        self.assertIn("did not load", err.getvalue())

    def test_ui_env_lists_tabs(self):
        import json
        j = json.loads(addons.ui_env({"exits": FAKE}, None))
        self.assertEqual(j, [{"name": "exits", "title": "Exits", "url": "http://127.0.0.1:1/#tok", "slow": ["connect"]}])

    def tearDown(self):
        with mock.patch.dict(os.environ, {"SRNET_NO_ADDONS": "1"}):
            addons.load(refresh=True)


if __name__ == "__main__":
    unittest.main()
