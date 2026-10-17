"""Setup preflight: every check reports PASS/WARN/FAIL/SKIP, the text form ends with PREFLIGHT=, and a report file is written."""
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor")]
from srnet import preflight  # noqa: E402


class Preflight(unittest.TestCase):
    def test_shape_and_report(self):
        with mock.patch.object(preflight, "c_demo", return_value=("WARN", "offline test", "hint")), \
             mock.patch.object(preflight, "c_clock", return_value=("PASS", "ok", "")):
            rep = preflight.run()
        names = [r["check"] for r in rep["checks"]]
        self.assertEqual(names, ["os", "admin", "wintun", "ports", "router", "dns", "demo", "clock", "disk", "firewall_av"])
        self.assertTrue(all(r["status"] in ("PASS", "WARN", "FAIL", "SKIP") for r in rep["checks"]))
        self.assertIn(rep["preflight"], ("OK", "WARN", "FAIL"))
        t = preflight.text(rep)
        self.assertIn("CHECK demo=WARN offline test", t)
        self.assertIn("FIX: hint", t)
        self.assertTrue(t.rstrip().endswith("PREFLIGHT=" + rep["preflight"]))
        p = preflight.write_report(rep, Path(tempfile.mkdtemp()) / "r.txt")
        self.assertIn("PREFLIGHT=", p.read_text())

    def test_broken_check_is_warning(self):
        with mock.patch.object(preflight, "CHECKS", [("x", lambda: 1 / 0)]):
            rep = preflight.run()
        self.assertEqual(rep["checks"][0]["status"], "WARN")


if __name__ == "__main__":
    unittest.main()
