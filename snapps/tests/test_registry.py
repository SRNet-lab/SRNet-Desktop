import json, shutil, tempfile, time, unittest
from pathlib import Path
from helpers import F
from typed import registry as R

TPL = Path(__file__).resolve().parent.parent / "typed" / "templates"


def files(text="v1"):
    return {"index.html": b"<!--STYLE--><div id=root></div><!--SCRIPTS-->",
            "app.js": f"/* {text} */".encode(), "tk.js": b"/*tk*/", "app.css": b"body{}"}


class Registry(unittest.TestCase):
    def setUp(self):
        self.key = F.new_key(); self.pin = F.pub_hex(self.key)
        self.bundled = Path(tempfile.mkdtemp()) / "b"
        (self.bundled / "demo").mkdir(parents=True)
        for n, b in files().items():
            (self.bundled / "demo" / n).write_bytes(b)
        self.e1 = R.sign_entry(self.key, "demo", "1.0.0", ["room.msg@1"], files())
        self.tm = R.TemplateManager(tempfile.mkdtemp(), self.pin, self.bundled, [self.e1])

    def test_pinned_bundle_loads(self):
        self.assertEqual(self.tm.load("demo")["entry"]["ver"], "1.0.0")

    def test_unpinned_or_tampered_never_runs(self):
        other = R.sign_entry(F.new_key(), "demo", "2.0.0", [], files("x"))
        with self.assertRaises(R.RegistryError):
            self.tm.offer(other, files("x"))
        e = R.sign_entry(self.key, "demo", "2.0.0", [], files("v2"))
        with self.assertRaises(R.RegistryError):
            self.tm.offer(e, files("evil"))  # files don't match signed hashes
        (self.bundled / "demo" / "app.js").write_bytes(b"/* tampered on disk */")
        self.assertIsNone(self.tm.load("demo"))
        bad = json.loads(json.dumps(e)); bad["entry"]["ver"] = "9.0.0"
        with self.assertRaises(R.RegistryError):
            R.verify_entry(bad, self.pin)

    def test_update_needs_approval_and_rollback_refused(self):
        e2 = R.sign_entry(self.key, "demo", "1.1.0", [], files("v2"))
        info = self.tm.offer(e2, files("v2"))
        self.assertTrue(info["needs_approval"])
        self.assertEqual(self.tm.load("demo")["entry"]["ver"], "1.0.0")  # not silent
        self.tm.approve("demo", "1.1.0")
        self.assertEqual(self.tm.load("demo")["entry"]["ver"], "1.1.0")
        with self.assertRaises(R.RegistryError):
            self.tm.offer(self.e1, files())  # rollback to 1.0.0
        with self.assertRaises(R.RegistryError):
            self.tm.offer(R.sign_entry(self.key, "demo", "1.1.0", [], files("v2")), files("v2"))

    def test_withdrawal_falls_back_and_allows_lower(self):
        e2 = R.sign_entry(self.key, "demo", "1.1.0", [], files("v2"))
        self.tm.offer(e2, files("v2")); self.tm.approve("demo", "1.1.0")
        w = R.sign_withdraw(self.key, 1, [{"template": "demo", "ver": "1.1.0", "reason": "bug"}])
        self.tm.apply_withdraw(w)
        t = self.tm.load("demo")
        self.assertEqual((t["entry"]["ver"], t.get("fallback")), ("1.0.0", True))
        with self.assertRaises(R.RegistryError):
            self.tm.apply_withdraw(w)  # stale / replayed withdraw list
        with self.assertRaises(R.RegistryError):
            self.tm.apply_withdraw(R.sign_withdraw(F.new_key(), 5, []))
        # installed version is withdrawn, so a fix with a lower-than-withdrawn version is ok
        e3 = R.sign_entry(self.key, "demo", "1.0.5", [], files("fix"))
        self.tm.offer(e3, files("fix")); self.tm.approve("demo", "1.0.5")
        self.assertEqual(self.tm.load("demo")["entry"]["ver"], "1.0.5")

    def test_expired_entry_refused(self):
        old = R.sign_entry(self.key, "demo", "3.0.0", [], files("o"), now=int(time.time()) - 400 * 86400)
        with self.assertRaises(R.RegistryError):
            self.tm.offer(old, files("o"))

    def test_shipped_registry_matches_bundles(self):
        reg = json.loads((TPL / "registry.json").read_text())
        for env in reg["entries"]:
            e = R.verify_entry(env, reg["pin"])
            self.assertEqual(R.hash_files(R.read_dir(TPL / e["template"])), e["files"], e["template"])


if __name__ == "__main__":
    unittest.main()
