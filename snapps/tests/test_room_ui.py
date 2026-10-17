"""Real-Chrome regression for the room template (needs playwright + Chrome; skipped otherwise):
- the room's Post button posts (via the core's sign sheet);
- typing a nick survives background re-renders: focus and text stay in the nick box."""
import json, os, shutil, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from helpers import F  # noqa: F401,E402
from typed import testbed as T  # noqa: E402
from typed.core import Core, serve as core_serve  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


def chrome():
    for c in (os.environ.get("CHROME"), shutil.which("google-chrome"), shutil.which("chrome"), shutil.which("chromium"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if c and os.path.exists(c):
            return c
    return None


CHROME = chrome()


@unittest.skipUnless(sync_playwright and CHROME, "playwright or Chrome not installed")
class RoomUI(unittest.TestCase):
    def test_post_button_and_nick_focus(self):
        env = T.start(mirror=False)
        try:
            core = Core(tempfile.mkdtemp(prefix="room-ui-"), T.client_config(env))
            srv = core_serve(core, 0, sync_every=0.3)  # re-render often, as a busy sync would
            base = f"http://127.0.0.1:{srv.server_address[1]}/"
            with sync_playwright() as p:
                b = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
                pg = b.new_page()
                pg.goto(base + "?c=lobby")
                fr = pg.frame_locator("#tpl")
                nick = fr.locator("input[placeholder=nick]")
                nick.wait_for(timeout=20000)
                # 1. nick: type slowly across several re-renders
                nick.click()
                for ch in "maxine":
                    pg.keyboard.type(ch)
                    time.sleep(0.25)
                time.sleep(1.0)
                frame = next(f for f in pg.frames if "/frame/" in f.url)
                self.assertEqual(fr.locator("input[placeholder=nick]").input_value(), "maxine")
                self.assertEqual(frame.evaluate("document.activeElement.placeholder"), "nick")
                # 2. Post button opens the core's sheet; Sign & post lands the line
                msg = fr.locator("input[placeholder^='Say']")
                msg.click()
                pg.keyboard.type("hello from the post button")
                fr.locator("button", has_text="Post").click()
                pg.wait_for_selector("#sheet:not([hidden])", timeout=10000)
                self.assertIn("hello from the post button", pg.inner_text("#sheet"))
                pg.click("#sheet-buttons button:has-text('Sign & post')")
                fr.locator(".item", has_text="hello from the post button").wait_for(timeout=20000)
                b.close()
            srv.shutdown()
        finally:
            T.stop(env)


if __name__ == "__main__":
    unittest.main()
