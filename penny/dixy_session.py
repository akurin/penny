"""Pin a Dixy session to the configured store.

Dixy answers the listing API for whichever store the session last chose, so a
fresh session (or one created by the captcha flow) can return another branch and
fail the address check. The picker flow below is the same one the shopping
project uses: open the site, type the address, accept the matching suggestion,
click «Выбрать», then keep the cookies.
"""
import os

from .logging_setup import event


def _click_address_control(page):
    page.evaluate(
        """() => {
            const e = [...document.querySelectorAll('*')].find(x =>
                /Укажи способ получения/i.test(x.textContent || '') && x.children.length === 0);
            if (!e) return;
            let a = e;
            for (let i = 0; i < 8 && a; i++, a = a.parentElement) {
                if (a.tagName === 'BUTTON' || a.tagName === 'A' ||
                    (a.className || '').toString().includes('btn')) { a.click(); return; }
            }
            e.click();
        }"""
    )


def _click_store(page, street):
    return page.evaluate(
        """(street) => {
            const buttons = [...document.querySelectorAll('button')]
                .filter(b => /Выбрать/.test(b.innerText || ''));
            for (const b of buttons) {
                let a = b;
                for (let i = 0; i < 7 && a; i++, a = a.parentElement) {
                    const t = a.innerText || '';
                    if (t.includes(street) && t.includes('Выбрать')) { b.click(); return true; }
                }
            }
            return false;
        }""",
        street)


def select_store(proxy, state_path, address):
    """Choose `address` as the pickup store and save the session.

    Raises RuntimeError when the site does not offer the store (a flagged exit
    shows the block page instead of the picker)."""
    address = (address or "").strip()
    if not address:
        raise ValueError("Dixy address is not configured (config/stores.local.json)")
    street = address.split(",")[-1].strip().split()[0]

    from .browser import _launch

    with _launch(True) as browser:
        context = browser.new_context(
            storage_state=state_path if os.path.exists(state_path) else None)
        try:
            page = context.new_page()
            page.goto("https://dixy.ru/", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(7000)
            if "403" in page.title():
                raise RuntimeError("Dixy answered the block page")
            _click_address_control(page)
            page.wait_for_timeout(2500)
            field = page.locator('input[placeholder*="Введи город"]').first
            field.click(timeout=8000)
            field.type(address, delay=120)
            page.wait_for_timeout(3500)
            suggestion = page.locator(".map-dixy-address__list-item", has_text=street)
            if not suggestion.count():
                raise RuntimeError("Dixy did not suggest the configured address")
            suggestion.first.click()
            page.wait_for_timeout(3500)
            if not _click_store(page, street):
                raise RuntimeError("Dixy did not show a matching «Выбрать» button")
            page.wait_for_timeout(5000)
            if street not in page.evaluate("document.body.innerText"):
                raise RuntimeError("Dixy did not keep the selected address")
            context.storage_state(path=state_path)
            event("store_session_selected", site="dixy")
        finally:
            context.close()
