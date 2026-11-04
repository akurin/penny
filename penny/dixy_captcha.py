"""Human-in-the-loop Дикси unblock from Telegram.

The bot never reads the SmartCaptcha image: it screenshots it and sends it to
the chat, a family member types the text, and the bot only fills the widget
and submits it. The human solve is the point — no OCR, no solving services.

Flow: open the block page -> click «Я не робот» -> pull the slider -> send the
image-text challenge -> wait for the answer -> submit -> save state/dixy.json.
"""
import asyncio
import json
import os
import random
import shutil
from urllib.parse import quote

from camoufox.async_api import AsyncCamoufox

from .browser import shared_headless_mode
from .logging_setup import event
from .store import dixy_search, ensure_ca_bundle, pause_site, reset_site_cooldown

SEARCH_URL = "https://dixy.ru/catalog/?q=" + quote("молоко")
CHALLENGE_TIMEOUT_S = 20
ANSWER_TIMEOUT_S = 180  # SmartCaptcha tokens are short-lived; do not wait longer
ANSWER_TRIES = 3
TOKEN_WAIT_S = 12
VERIFY_RETRY_S = 5  # the edge allow propagates asynchronously; poll, don't panic
VERIFY_TRIES = 12  # ~1 minute
VERIFY_PAUSE_S = 180  # if it still lags, stay quiet instead of re-blocking

POST_TOKEN = """async (token) => {
    const r = await fetch('/error/ajax.php', {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: 'code=403&token=' + encodeURIComponent(token),
        credentials: 'include'});
    return {status: r.status, body: await r.text()};
}"""


async def _wait_for(frame_getter, timeout_s, step_s=0.7):
    """Poll an async getter until it returns something truthy or time runs out."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while loop.time() < end:
        found = await frame_getter()
        if found:
            return found
        await asyncio.sleep(step_s)
    return None


def _widget_frames(page, marker):
    return [f for f in page.frames if "smartcaptcha" in f.url and marker in f.url]


async def _find_thumb(page):
    for frame in _widget_frames(page, "checkbox"):
        try:
            thumb = await frame.query_selector('[data-testid="thumb"]')
        except Exception:
            continue
        if thumb:
            return frame, thumb
    return None


async def _find_challenge(page):
    frames = _widget_frames(page, "advanced")
    return frames[0] if frames else None


async def _pull_slider(page, frame, thumb):
    """Pull the slider like a person: slow jittered drag to ~95% of the track."""
    handle = await thumb.bounding_box()
    track = await (await frame.query_selector('[data-testid="slider"]')).bounding_box()
    cx = handle["x"] + handle["width"] / 2
    cy = handle["y"] + handle["height"] / 2
    target = track["x"] + track["width"] * 0.95
    await page.mouse.move(cx, cy)
    await page.wait_for_timeout(600)
    await page.mouse.down()
    total = target - cx
    for step in range(1, 41):
        await page.mouse.move(cx + total * (step / 40) + random.uniform(-1, 1),
                              cy + random.uniform(-0.8, 0.8))
        await page.wait_for_timeout(random.randint(15, 45))
    await page.wait_for_timeout(300)
    await page.mouse.up()


async def _wait_token(page):
    async def read():
        return await page.evaluate(
            """() => {
                const t = document.querySelector('input[name="smart-token"]');
                return t ? t.value : '';
            }""")
    return await _wait_for(read, TOKEN_WAIT_S, 0.6)


async def _save_state(context, state_path):
    if os.path.exists(state_path):
        shutil.copy2(state_path, state_path + ".bak")
    await context.storage_state(path=state_path)


def _verify_http(proxy, state_path):
    """Confirm the saved session works over the ordinary HTTP transport."""
    try:
        return len(dixy_search("молоко", proxy, state_path=state_path)["products"])
    except Exception:
        return None


async def unblock(proxy, state_path, send_image, get_answer, notify):
    """Run the captcha relay; returns (English result code, Russian chat message).

    send_image(png_bytes), get_answer() -> str|None, notify(text) are async.
    """
    ensure_ca_bundle()
    async with AsyncCamoufox(headless=shared_headless_mode(), os="macos",
                             humanize=True, block_webrtc=True, geoip=True,
                             proxy={"server": proxy}) as browser:
        context = await browser.new_context(
            storage_state=state_path if os.path.exists(state_path) else None)
        try:
            page = await context.new_page()
            await page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)
            if await page.title() != "403":
                await _save_state(context, state_path)
                event("dixy_captcha", result="already_allowed")
                return "already_allowed", "Дикси уже отвечает — капча не понадобилась."
            clicked = False
            for frame in _widget_frames(page, "checkbox"):
                button = await frame.query_selector(".CaptchaButton")
                if button:
                    await button.hover()
                    await page.wait_for_timeout(800)
                    await button.click()
                    clicked = True
                    break
            if not clicked:
                return "no_widget", "Не нашла кнопку «Я не робот» на странице Дикси."
            await page.wait_for_timeout(1500)
            if await page.title() != "403":
                await _save_state(context, state_path)
                event("dixy_captcha", result="checkbox_passed")
                return "checkbox_passed", "Капча пройдена одним нажатием — Дикси разблокирован."
            thumb = await _wait_for(lambda: _find_thumb(page), CHALLENGE_TIMEOUT_S)
            if not thumb:
                return ("no_slider", "Капча не показала ползунок — разблокируйте на Mac: "
                                     "python -m penny.dixy_unblock.")
            frame, handle = thumb
            await _pull_slider(page, frame, handle)
            challenge = await _wait_for(lambda: _find_challenge(page), CHALLENGE_TIMEOUT_S)
            if not challenge:
                if await page.title() != "403":
                    await _save_state(context, state_path)
                    event("dixy_captcha", result="slider_passed")
                    return "slider_passed", "Капча пройдена ползунком — Дикси разблокирован."
                return ("no_challenge", "Капча не показала картинку — разблокируйте на Mac: "
                                        "python -m penny.dixy_unblock.")
            for attempt in range(1, ANSWER_TRIES + 1):
                image = (await challenge.query_selector("img")
                         or await challenge.query_selector("canvas"))
                if not image:
                    return "no_image", "Не нашла картинку капчи — попробуйте позже."
                await send_image(await image.screenshot())
                answer = await get_answer()
                if not answer:
                    return "answer_timeout", "Не дождалась ответа — капча не пройдена."
                field = await challenge.query_selector("input")
                submit = await challenge.query_selector("button.CaptchaButton_view_action")
                if not field or not submit:
                    return ("widget_changed", "Капча поменяла вид — разблокируйте на Mac: "
                                              "python -m penny.dixy_unblock.")
                await field.fill(answer)
                await submit.click()
                token = await _wait_token(page)
                if not token:
                    challenge = await _wait_for(lambda: _find_challenge(page), 8) or challenge
                    if attempt < ANSWER_TRIES:
                        await notify("Не подошло — вот новая картинка.")
                    continue
                result = await page.evaluate(POST_TOKEN, token)
                try:
                    allowed = bool(json.loads(result.get("body") or "{}").get("allow"))
                except ValueError:
                    allowed = False
                if not allowed:
                    return "rejected", "Дикси отклонил проверку — попробуйте позже."
                await _save_state(context, state_path)
                reset_site_cooldown("dixy")
                event("dixy_captcha", result="solved")
                for attempt in range(VERIFY_TRIES):
                    products = await asyncio.to_thread(_verify_http, proxy, state_path)
                    if products is not None:
                        return ("verified",
                                f"Дикси разблокирован: {products} товаров в проверочном поиске.")
                    if attempt < VERIFY_TRIES - 1:
                        await asyncio.sleep(VERIFY_RETRY_S)
                pause_site("dixy", VERIFY_PAUSE_S)
                return ("allow_pending", "Капча пройдена и cookie сохранены, но Дикси ещё не пускает "
                                         "запросы с этого выхода. Попробуйте запрос через пару минут.")
            return ("attempts_exhausted", "Не удалось пройти капчу за несколько попыток — разблокируйте "
                                          "на Mac: python -m penny.dixy_unblock.")
        finally:
            await context.close()
