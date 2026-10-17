"""`srnet open`: the srnet:// handler validates links and only opens the join sheet."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from srnet import cli  # noqa: E402

LINK = "srnet://" + "y" * 52 + ".sesh/room/lobby#root=" + "ab" * 32


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as h, redirect_stdout(out), redirect_stderr(err):
        import os
        os.environ["SRNET_HOME"] = h
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class OpenLink(unittest.TestCase):
    def test_valid_link_goes_to_join_sheet(self):
        rc, out, _ = run("open", LINK, "--print-only")
        self.assertEqual(rc, 0)
        u = urlparse(out.strip())
        self.assertEqual((u.scheme, u.netloc, u.path), ("http", "127.0.0.1:48920", "/"))
        self.assertEqual(parse_qs(u.query)["join"], [LINK])

    def test_bad_links_refused(self):
        for bad in ("http://evil.example/", "srnet://evil.example/room/x", LINK + "\"&calc", "javascript:alert(1)"):
            rc, out, err = run("open", bad, "--print-only")
            self.assertEqual(rc, 2, bad)
            self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
