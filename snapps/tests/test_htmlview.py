import unittest
from helpers import F
from typed import htmlview as V
from typed.testbed import HOSTILE_HTML

VECTORS = [
    '<script>alert(1)</script>', '<img src=x onerror=alert(1)>', '<a href="javascript:alert(1)">x</a>',
    '<a href="JaVaScRiPt:alert(1)">x</a>', '<a href=" javascript:alert(1)">x</a>',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>', '<svg onload=alert(1)>',
    '<iframe src="http://e"></iframe>', '<object data=x></object>', '<embed src=x>',
    '<meta http-equiv=refresh content="0;url=http://e">', '<style>@import "http://e"</style>',
    '<link rel=stylesheet href=http://e>', '<p style="background:url(http://e)">x</p>',
    '<base href="http://e/">', '<form action=http://e><button>go</button></form>',
    '<math><mtext><script>alert(1)</script></mtext></math>', '<p onmouseover=alert(1)>x</p>',
    '<div><a href=x onclick=alert(1)>a</a></div>', '<!--<script>alert(1)</script>-->',
    '<scr<script>ipt>alert(1)</script>', '<template><script>alert(1)</script></template>',
    '<noscript><p title="</noscript><img src=x onerror=alert(1)>"></noscript>',
    '&lt;script&gt;alert(1)&lt;/script&gt;', '<a href="http://example.com">c</a>',
    '<video src=http://e></video>', '<audio src=x>', '<textarea><script>1</script></textarea>',
]
FORBIDDEN = ["<script", "onerror", "onload", "onclick", "onmouseover", "javascript:", "<iframe",
             "<object", "<embed", "<meta", "<style", "<link", "style=", "<base", "<form", "<svg",
             "<math", "data:text", "http://e", "<img", "<video", "<audio", "<template"]


class Viewer(unittest.TestCase):
    def test_vectors(self):
        for v in VECTORS:
            out = V.sanitize(v, "abc.sesh").lower()
            for f in FORBIDDEN:
                self.assertNotIn(f, out, f"{v!r} -> {out!r}")

    def test_links(self):
        out = V.sanitize('<a href="/about.html">a</a><a href="http://xyz.sesh/p">b</a>'
                         '<a href="https://example.com/">c</a>', "abc.sesh")
        self.assertIn('href="/viewer?name=abc.sesh%2Fabout.html"', out)
        self.assertIn('href="/viewer?name=xyz.sesh%2Fp"', out)
        self.assertIn("clearnet link blocked: example.com", out)
        self.assertNotIn("https://example.com", out)

    def test_page_has_banner_and_strict_csp(self):
        page = V.render(HOSTILE_HTML, "demo.sesh")
        self.assertIn("Untyped site · demo.sesh · not verified content", page)
        self.assertLess(page.index("srnet-banner"), page.index("srnet-page"))
        self.assertEqual(page.count("<style>"), 1)  # only the viewer's own CSS
        for d in ("script-src 'none'", "connect-src 'none'", "img-src 'none'", "frame-src 'none'",
                  "form-action 'none'", "sandbox"):
            self.assertIn(d, V.CSP)
        self.assertIn("<h1>Plain page</h1>", page)

    def test_text_is_escaped(self):
        self.assertEqual(V.sanitize("<p>a &lt;b&gt; &amp; \"q\"</p>", "x.sesh"),
                         "<p>a &lt;b&gt; &amp; &quot;q&quot;</p>")


if __name__ == "__main__":
    unittest.main()
