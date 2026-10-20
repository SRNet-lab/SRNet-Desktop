"""install.sh with a stand-in Python: options, the add-on source and the pre-split venv cleanup.
Nothing is downloaded; the stand-in only logs what the installer asked for."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

FAKE_PY = r'''#!/usr/bin/env bash
# stand-in python: logs calls; "-m venv D" makes D/bin/python a copy of this script
echo "$*" >> "$FAKE_LOG"
case "$*" in *"srnet setup-router"*) exit "${FAKE_ROUTER_RC:-0}";; esac
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; exit 0; fi
if [ "$1" = "-c" ]; then
  case "$2" in
    *find_spec*) for m in $FAKE_MODULES; do case "$2" in *"'$m'"*) exit 0;; esac; done; exit 1;;
  esac
  exit 0
fi
exit 0
'''


@unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "install.sh is for Linux/macOS")
class InstallSh(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.py = self.tmp / "python3"
        self.py.write_text(FAKE_PY)
        self.py.chmod(0o755)
        self.log = self.tmp / "log"
        self.prefix, self.bin = self.tmp / "prefix", self.tmp / "bin"

    def run_install(self, *args, modules="", **extra):
        self.log.write_text("")
        env = dict(os.environ, PYTHON=str(self.py), FAKE_LOG=str(self.log), FAKE_MODULES=modules,
                   SRNET_PREFIX=str(self.prefix), SRNET_BIN=str(self.bin), HOME=str(self.tmp / "home"),
                   SRNET_ROUTER_INI=str(self.tmp / "client.ini"), **extra)
        env.pop("SRNET_EXITS_SRC", None)
        env.pop("SRNET_SKIP_ROUTER_SETUP", None)
        return subprocess.run(["bash", str(REPO / "install.sh"), *args], env=env, capture_output=True, text=True)

    def pip_installs(self):
        return [l for l in self.log.read_text().splitlines() if l.startswith("-m pip install")]

    def test_unknown_option_is_refused(self):
        r = self.run_install("--with-exit")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option --with-exit", r.stderr)
        self.assertFalse(self.prefix.exists())

    def test_plain_install_has_no_addon(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.bin / "srnet").exists())
        self.assertFalse(any("srnet-exits" in l for l in self.pip_installs()))

    def test_with_exits_installs_from_git_not_by_bare_name(self):
        r = self.run_install("--with-exits")
        self.assertEqual(r.returncode, 0, r.stderr)
        addon = [l for l in self.pip_installs() if "srnet-exits" in l]
        self.assertEqual(len(addon), 1)
        self.assertIn("srnet-exits @ git+https://github.com/SRNet-lab/", addon[0])
        self.assertFalse(any(l.split()[-1] in ("srnet-client", "srnet-exits") for l in self.pip_installs()))

    def router_calls(self):
        return [l for l in self.log.read_text().splitlines() if "setup-router" in l]

    @unittest.skipUnless(sys.platform.startswith("linux"), "router setup is Linux-only")
    def test_with_exits_sets_up_the_router_once(self):
        (self.tmp / "client.ini").write_text("[network]\n")
        r = self.run_install("--with-exits")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.router_calls(), [f"-m srnet setup-router --ini {self.tmp / 'client.ini'}"])
        self.assertIn("sudo asks for your password once", r.stdout)
        self.assertIn("Session Router: set up for exits", r.stdout)

    @unittest.skipUnless(sys.platform.startswith("linux"), "router setup is Linux-only")
    def test_router_setup_failure_does_not_fail_the_install(self):
        (self.tmp / "client.ini").write_text("[network]\n")
        r = self.run_install("--with-exits", FAKE_ROUTER_RC="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not set up for exits yet; run: srnet setup-router", r.stdout)

    def test_no_router_setup_without_exits_or_without_session_router(self):
        (self.tmp / "client.ini").write_text("[network]\n")
        self.run_install()
        self.assertEqual(self.router_calls(), [])
        (self.tmp / "client.ini").unlink()
        r = self.run_install("--with-exits")
        self.assertEqual(self.router_calls(), [])
        if sys.platform.startswith("linux"):
            self.assertIn("After installing Session Router, run once: srnet setup-router", r.stdout)

    def test_plain_upgrade_does_not_ask_sudo(self):
        (self.tmp / "client.ini").write_text("[network]\n")
        self.run_install("--with-exits")
        self.run_install(modules="srnet_exits")
        self.assertEqual(self.router_calls(), [])

    def test_path_goes_into_bashrc_once_and_zshrc_only_if_present(self):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        (home / ".bashrc").write_text("# mine\nalias ll='ls -l'\n")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        rc = (home / ".bashrc").read_text()
        self.assertTrue(rc.startswith("# mine\nalias ll='ls -l'\n"))
        self.assertEqual(rc.count("added by SRNet-Desktop install.sh"), 1)
        self.assertIn(f'export PATH="{self.bin}:$PATH"', rc)
        self.assertIn("Added", r.stdout)
        self.assertFalse((home / ".zshrc").exists())
        # the line works in a fresh bash
        out = subprocess.run(["bash", "-c", f"PATH=/usr/bin:/bin; . {home / '.bashrc'}; echo $PATH"],
                             capture_output=True, text=True).stdout
        self.assertIn(str(self.bin), out)
        (home / ".zshrc").write_text("")
        r = self.run_install()
        self.assertEqual((home / ".bashrc").read_text(), rc, "not added twice")
        self.assertEqual((home / ".zshrc").read_text().count("added by SRNet-Desktop install.sh"), 1)
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SRNet-Desktop install.sh", (home / ".bashrc").read_text())
        self.assertIn("alias ll='ls -l'", (home / ".bashrc").read_text())
        self.assertNotIn("SRNet-Desktop install.sh", (home / ".zshrc").read_text())

    def test_old_install_with_wallet_libs_gets_a_clean_venv(self):
        self.run_install()
        marker = self.prefix / "venv" / "old-file"
        marker.write_text("x")
        (self.bin / "srnet-relayer").write_text("old")
        r = self.run_install(modules="eth_account")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Removing the old client's wallet libraries", r.stdout)
        self.assertFalse(marker.exists())
        self.assertFalse((self.bin / "srnet-relayer").exists())

    def test_upgrade_keeps_an_installed_addon(self):
        self.run_install()
        r = self.run_install(modules="srnet_exits")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any("srnet-exits @ git+" in l for l in self.pip_installs()))
        self.assertNotIn("Removing the old", r.stdout)


if __name__ == "__main__":
    unittest.main()
