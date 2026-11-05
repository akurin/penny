"""Unblock Дикси's SmartCaptcha by hand, then refresh state/dixy.json.

Dixy answers a flagged proxy exit with a Yandex SmartCaptcha chain
(checkbox -> slider -> image-text challenge). The final image step is a real
visual captcha and stays out of automation by design — a human solves it once
here, the server-side allow lands on the session, and the regular HTTP
transport (penny.store.dixy_search) keeps working with the refreshed cookies.

Run this on a machine with a display (the Mac, not the headless VM):

    STORE_PROXY=socks5://127.0.0.1:10807 ./scripts/uv run --locked python -m penny.dixy_unblock

The window opens dixy.ru through the proxy. Solve the «Я не робот» check when
it appears; the script saves the refreshed storage state over state/dixy.json
(the previous file is kept as state/dixy.json.bak) and verifies one search.
"""
import json
import os
import shutil
from urllib.parse import quote

from dotenv import load_dotenv

from .logging_setup import event
from .store import DIXY_STATE_PATH, ensure_ca_bundle, normalize_dixy, verify_dixy_branch


def main():
    load_dotenv()
    from .store import load_store_config
    load_store_config(os.getenv("PENNY_STORES_CONFIG") or None)
    ensure_ca_bundle()
    proxy = os.getenv("STORE_PROXY")
    if not proxy:
        raise SystemExit("STORE_PROXY is missing")
    state_path = DIXY_STATE_PATH
    from camoufox.sync_api import Camoufox
    captured = {}

    def on_response(response):
        if "listing-json.php" in response.url and response.status == 200 and "data" not in captured:
            try:
                captured["data"] = json.loads(response.text())
            except Exception:
                pass

    with Camoufox(headless=False, os="macos", humanize=True, block_webrtc=True,
                  geoip=True, proxy={"server": proxy}) as browser:
        context = browser.new_context(
            storage_state=state_path if os.path.exists(state_path) else None)
        page = context.new_page()
        page.on("response", on_response)
        page.goto("https://dixy.ru/catalog/?q=" + quote("молоко"),
                  wait_until="domcontentloaded", timeout=60000)
        print("Window opened. If you see the «Я не робот» page, solve the captcha by hand")
        print("and press «Подтвердить»; the site reloads in about a minute.")
        print("Waiting for the catalogue (up to 10 minutes)...")
        blocked = True
        for _ in range(600):
            page.wait_for_timeout(1000)
            try:
                if page.title() != "403":
                    blocked = False
                    break
            except Exception:
                pass  # navigation can tear the page down mid-poll
        if blocked:
            page.screenshot(path="/tmp/dixy_unblock_failed.png")
            print("Captcha was not solved — state not saved. Screenshot: /tmp/dixy_unblock_failed.png")
            raise SystemExit(1)
        if os.path.exists(state_path):
            shutil.copy2(state_path, state_path + ".bak")
        context.storage_state(path=state_path)
        event("dixy_unblocked", state="saved")
        print("state/dixy.json updated (previous file kept as dixy.json.bak).")
        # One real search through the page to confirm the session is allowed.
        captured.clear()
        page.goto("https://dixy.ru/catalog/?q=" + quote("молоко"),
                  wait_until="domcontentloaded", timeout=60000)
        for _ in range(20):
            page.wait_for_timeout(1000)
            if "data" in captured:
                break
        if "data" not in captured:
            print("Catalogue opened but the listing response was not captured — check the site by hand.")
            raise SystemExit(1)
        try:
            result = verify_dixy_branch(normalize_dixy(captured["data"], "молоко"))
        except Exception as exc:
            print(f"Response received but branch verification failed: {type(exc).__name__}.")
            print("The session may point at another store — re-run examples/dixy_select_store.py from ../shopping.")
            raise SystemExit(1)
        address = result.get("address") or "?"
        print(f"Check passed: {len(result['products'])} candidates, store: {address}")
        print("The regular Penny bot now fetches Дикси over HTTP again.")
    print("No Telegram messages or DeepSeek requests were sent.")


if __name__ == "__main__":
    main()
