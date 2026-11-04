import os
import tempfile
import unittest
from unittest.mock import patch

from penny import dixy_captcha


class FakePage:
    def __init__(self, title):
        self._title = title
        self.frames = []

    async def goto(self, *args, **kwargs):
        pass

    async def wait_for_timeout(self, milliseconds):
        pass

    async def title(self):
        return self._title


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.saved = None
        self.closed = False

    async def new_page(self):
        return self.page

    async def storage_state(self, path):
        self.saved = path

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context

    async def new_context(self, **kwargs):
        return self.context


class FakeCamoufox:
    browser = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return type(self).browser

    async def __aexit__(self, *args):
        return False


class FakeElement:
    def __init__(self, box=None):
        self.box = box
        self.value = None

    async def hover(self):
        pass

    async def click(self):
        pass

    async def fill(self, value):
        self.value = value

    async def screenshot(self):
        return b"png-bytes"

    async def bounding_box(self):
        return self.box


class FakeFrame:
    def __init__(self, url, elements):
        self.url = url
        self.elements = elements

    async def query_selector(self, selector):
        return self.elements.get(selector)


class FakeMouse:
    async def move(self, *args):
        pass

    async def down(self):
        pass

    async def up(self):
        pass


class FakeChallengePage(FakePage):
    def __init__(self):
        super().__init__("403")
        box = {"x": 0, "y": 0, "width": 100, "height": 20}
        checkbox = FakeFrame("https://smartcaptcha.cloud.yandex.ru/checkbox.ru.html", {
            ".CaptchaButton": FakeElement(box),
            '[data-testid="thumb"]': FakeElement(box),
            '[data-testid="slider"]': FakeElement(box),
        })
        self.advanced = FakeFrame("https://smartcaptcha.cloud.yandex.ru/advanced.ru.html", {
            "img": FakeElement(box),
            "input": FakeElement(box),
            "button.CaptchaButton_view_action": FakeElement(box),
        })
        self.frames = [checkbox, self.advanced]
        self.mouse = FakeMouse()

    async def evaluate(self, script, arg=None):
        if "smart-token" in script:
            return "TOKEN"
        return {"status": 200, "body": '{"allow": true}'}


class DixyCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, title, state_path, success=False, verify=5):
        page = FakeChallengePage() if success else FakePage(title)
        context = FakeContext(page)
        FakeCamoufox.browser = FakeBrowser(context)
        asked = []

        async def send_image(png):
            asked.append(png)

        async def get_answer():
            return "answer" if success else ""

        async def notify(text):
            pass

        async def inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        if callable(verify):
            side = verify
        elif isinstance(verify, list):
            pending = iter(verify)
            def side(*args, **kwargs):
                return next(pending, None)
        else:
            def side(*args, **kwargs):
                return verify
        with patch.object(dixy_captcha, "AsyncCamoufox", FakeCamoufox), \
                patch.object(dixy_captcha, "shared_headless_mode", return_value=True), \
                patch.object(dixy_captcha, "ensure_ca_bundle"), \
                patch.object(dixy_captcha, "_verify_http", side_effect=side), \
                patch.object(dixy_captcha.asyncio, "to_thread", new=inline), \
                patch.object(dixy_captcha, "reset_site_cooldown") as reset, \
                patch.object(dixy_captcha, "pause_site") as pause, \
                patch.object(dixy_captcha, "VERIFY_RETRY_S", 0):
            result_code, status = await dixy_captcha.unblock(
                "socks5://127.0.0.1:1", state_path, send_image, get_answer, notify)
        return result_code, status, asked, context, reset, pause, page

    async def test_solved_challenge_saves_state_and_verifies(self):
        with tempfile.TemporaryDirectory() as folder:
            state = os.path.join(folder, "dixy.json")
            code, status, asked, context, reset, pause, page = await self._run(
                "403", state, success=True)
            self.assertEqual(code, "verified")
            self.assertIn("разблокирован: 5 товаров", status)
            self.assertEqual(len(asked), 1)
            self.assertEqual(context.saved, state)
            reset.assert_called_once_with("dixy")
            pause.assert_not_called()
            self.assertEqual(page.advanced.elements["input"].value, "answer")

    async def test_solve_waits_for_the_edge_allow_to_propagate(self):
        with tempfile.TemporaryDirectory() as folder:
            state = os.path.join(folder, "dixy.json")
            code, status, _, _, _, pause, _ = await self._run(
                "403", state, success=True, verify=[None, None, 7])
            self.assertEqual(code, "verified")
            self.assertIn("разблокирован: 7 товаров", status)
            pause.assert_not_called()

    async def test_lagging_allow_pauses_instead_of_reblocking(self):
        with tempfile.TemporaryDirectory() as folder:
            state = os.path.join(folder, "dixy.json")
            code, status, _, _, _, pause, _ = await self._run(
                "403", state, success=True, verify=lambda *a, **k: None)
            self.assertEqual(code, "allow_pending")
            self.assertIn("через пару минут", status)
            pause.assert_called_once_with("dixy", dixy_captcha.VERIFY_PAUSE_S)

    async def test_already_allowed_saves_state_without_asking(self):
        with tempfile.TemporaryDirectory() as folder:
            state = os.path.join(folder, "dixy.json")
            with open(state, "w") as file:
                file.write("{}")
            code, status, asked, context, _, _, _ = await self._run("Дикси — каталог", state)
            self.assertEqual(code, "already_allowed")
            self.assertIn("уже отвечает", status)
            self.assertEqual(asked, [])
            self.assertEqual(context.saved, state)
            self.assertTrue(context.closed)
            self.assertTrue(os.path.exists(state + ".bak"))

    async def test_blocked_page_without_widget_reports_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            state = os.path.join(folder, "dixy.json")
            code, status, asked, context, _, _, _ = await self._run("403", state)
            self.assertEqual(code, "no_widget")
            self.assertIn("Не нашла кнопку", status)
            self.assertEqual(asked, [])
            self.assertIsNone(context.saved)
            self.assertTrue(context.closed)


if __name__ == "__main__":
    unittest.main()
