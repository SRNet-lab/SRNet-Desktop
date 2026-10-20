"""Shipped text files only describe code that is in this repo."""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class RepoFiles(unittest.TestCase):
    def test_license_has_no_references_to_removed_code(self):
        text = (REPO / "LICENSE").read_text()
        for gone in ("vendor/", "Solidity", "wallet", "relayer", "top-up"):
            self.assertNotIn(gone, text)

    def test_desktop_entry_label(self):
        self.assertIn("Comment=SNApps for SRNet\n", (REPO / "client-cli" / "srnet-ui.desktop").read_text())

    def test_readme_links_point_at_files_that_exist(self):
        import re
        for link in re.findall(r"\]\(((?!https?:|#|mailto:)[^)]+)\)", (REPO / "README.md").read_text()):
            self.assertTrue((REPO / link.split("#")[0]).exists(), link)


if __name__ == "__main__":
    unittest.main()
