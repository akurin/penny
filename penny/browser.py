"""Bounded Camoufox worker for stores that reject plain HTTP.

One browser process per display mode, each on its own playwright thread: 5ka
runs plain headless, Lenta needs the WebGL-capable "virtual" mode. States come
from pricehound/state/<site>.json (Playwright storage_state, refreshed by the
shopping login flows). Every site keeps its verified in-page fetch path from
../shopping/examples; nothing here invents new bypasses. Дикси deliberately
stays on the HTTP transport (penny.store.dixy_search): a flagged exit gets
SmartCaptcha's checkbox -> slider -> image-text chain, whose final step is not
automatable by design, so the browser would only burn the shared budget.
Unblock by hand with penny.dixy_unblock instead.
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .logging_setup import event
from .store_config import SITES
from .store import ServiceBlocked

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")

_browsers = {}  # mode key -> (manager, browser): 5ka headless, Lenta virtual
_site_contexts = {}  # site -> long-lived context (5ka keeps its clearance)
# One lock per browser process. Playwright's sync API must be called from the
# thread that owns the browser, and every headless mode owns one; a single
# global lock also serialized 5ka and Lenta, which share nothing.
_browser_locks = {key: threading.Lock() for key in ("headless", "virtual", "headed")}


def _site_lock(site):
    return _browser_locks[_browser_key(headless_mode(site))]
# Playwright's sync API is bound to the thread that started it, and two sync
# Camoufox instances cannot share one thread (the first leaves a running loop
# behind). Each headless mode therefore gets its own single browser thread:
# 5ka is plain headless, Lenta needs the WebGL-capable virtual mode.
BROWSER_EXECUTORS = {
    "headless": ThreadPoolExecutor(max_workers=1, thread_name_prefix="penny-browser-headless"),
    "virtual": ThreadPoolExecutor(max_workers=1, thread_name_prefix="penny-browser-virtual"),
}
# Research sets one wall-clock deadline for all serialized browser searches in
# a round.  A ContextVar keeps concurrent Telegram requests isolated and is
# explicitly propagated when work is submitted to a browser thread.
BROWSER_DEADLINE = ContextVar("penny_browser_deadline", default=None)


def _browser_key(mode):
    return "virtual" if mode == "virtual" else "headless" if mode else "headed"


def site_executor(site):
    return BROWSER_EXECUTORS[_browser_key(headless_mode(site))]


def run_browser_task(fn, *args, site=None, mode=None, **kwargs):
    """Run a browser job on the thread that owns the site's headless mode."""
    key = mode or (_browser_key(headless_mode(site)) if site else "virtual")
    return BROWSER_EXECUTORS[key].submit(fn, *args, **kwargs).result()


class BrowserUnavailable(RuntimeError):
    """Camoufox or its browser binary is missing; the site degrades to a note."""


def headless_mode(site):
    """5ka needs plain headless (ServicePipe blocks the Xvfb mode); Lenta's
    Qrator needs a real WebGL context: 'virtual' (Xvfb) on Linux, headed on
    macOS. One browser process per mode serves that site."""
    override = os.getenv("PENNY_BROWSER_HEADLESS")
    if override in ("true", "false", "virtual"):
        return {"true": True, "false": False, "virtual": "virtual"}[override]
    if site == "lenta":
        import platform
        return "virtual" if platform.system() == "Linux" else False
    return True


def shared_headless_mode():
    """The strictest mode (Lenta's); kept for the README, tests and warm-up."""
    return headless_mode("lenta")


def _launch(mode=None):
    from camoufox.sync_api import Camoufox
    if mode is None:
        mode = shared_headless_mode()
    return Camoufox(headless=mode, os="macos", humanize=True, block_webrtc=True,
                    geoip=True, proxy={"server": os.environ["STORE_PROXY"]})


def get_browser(site):
    """Lazy singleton per headless mode; restarts after a crash. Raises
    BrowserUnavailable with a safe hint when the browser binary is missing."""
    mode = headless_mode(site)
    key = _browser_key(mode)
    entry = _browsers.get(key)
    if entry is not None:
        try:
            if entry[1].is_connected():
                return entry[1]
        except Exception:
            pass
        _close_one(key)
    manager = None
    try:
        manager = _launch(mode)
        browser = manager.__enter__()
        _browsers[key] = (manager, browser)
        event("browser_started", requested_by=site, headless=str(mode))
        return browser
    except Exception as exc:
        if manager is not None:
            try:
                manager.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
        _browsers.pop(key, None)
        event("browser_start_failed", 30)
        raise BrowserUnavailable(
            "Browser unavailable: run `python -m camoufox fetch` (Lenta on Linux also needs Xvfb)."
        ) from exc


def _close_one(key):
    entry = _browsers.pop(key, None)
    if entry is not None:
        try:
            entry[0].__exit__(None, None, None)
        except Exception:
            pass
        event("browser_closed", mode=key)


def close_browser(mode=None):
    """Close one mode's browser and contexts on the thread that owns it."""
    key = mode or "virtual"
    for site in [s for s in _site_contexts if _browser_key(headless_mode(s)) == key]:
        _drop_context(site)
    _close_one(key)


def close_all_browsers(timeout=1.5):
    """Shut every mode down on its own thread, but never hold exit for long.

    A search can be mid-flight on a browser thread; waiting for it to finish is
    what made Ctrl+C take tens of seconds. Both close jobs are submitted at
    once, so an idle mode still closes while a busy one is being waited on.
    The timeout is one total budget, not one per mode; then queued work is
    dropped and the process exits, leaving the OS to reap the browser children."""
    futures = {key: executor.submit(close_browser, key)
               for key, executor in list(BROWSER_EXECUTORS.items())}
    deadline = time.monotonic() + max(0.0, timeout)
    for key, future in futures.items():
        try:
            future.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            event("browser_close_skipped", mode=key)
    for executor in BROWSER_EXECUTORS.values():
        executor.shutdown(wait=False, cancel_futures=True)


def _state_path(site):
    return os.path.join(STATE_DIR, f"{site}.json")


def _context(browser, site):
    state = _state_path(site)
    kwargs = {}
    if os.path.exists(state):
        kwargs["storage_state"] = state
    return browser.new_context(**kwargs)


def _persistent_context(browser, site):
    """One long-lived context per site, so a cleared anti-bot session survives.

    ServicePipe / Qrator clearances are bound to this browser's fingerprint and
    cookies; opening a fresh context for every search re-solves the challenge
    and looks like a burst. The caller keeps the page open as well."""
    context = _site_contexts.get(site)
    if context is not None:
        try:
            context.pages  # raises when the context died with the browser
            return context
        except Exception:
            _drop_context(site)
    context = _context(browser, site)
    _site_contexts[site] = context
    return context


def _drop_context(site):
    context = _site_contexts.pop(site, None)
    if context is not None:
        try:
            context.close()
        except Exception:
            pass


def _deadline(max_seconds):
    own = time.time() + max_seconds
    shared = BROWSER_DEADLINE.get()
    return min(own, shared) if shared is not None else own


def _timeout_ms(deadline, cap_seconds=15):
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError("browser round deadline exceeded")
    return max(1, int(min(remaining, cap_seconds) * 1000))


FIVEKA_FETCH = """async (url) => {
    try {
        const r = await fetch(url, {credentials: "include"});
        return {status: r.status, body: await r.text()};
    } catch (e) {
        return {status: 0, body: String(e)};
    }
}"""


def _evaluate_retrying(page, script, arg, deadline_ts, step_ms=1000):
    """ServicePipe/Qrator reload the page mid-evaluate; retry until deadline."""
    from playwright.sync_api import Error as PWError
    result = None
    while time.time() < deadline_ts:
        try:
            result = page.evaluate(script, arg)
            return result
        except PWError:
            page.wait_for_timeout(step_ms)
    return result


FIVEKA_DEADLINE_S = 12  # the ServicePipe challenge settles in ~2 s; a stuck
                        # fetch must not eat the whole round (lenta and the
                        # follow-up queries wait behind it on the one thread)


def _fiveka_blocked(page):
    """ServicePipe's static block page (not the JS challenge) means the exit is banned."""
    try:
        return page.evaluate(
            """() => document.body ? document.body.innerText.includes('Проблемы со связью') : false""")
    except Exception:
        return False


def fiveka_browser_search(query, proxy=None, store=None):
    """5ka via the in-page ServicePipe fetch; store rides the URL path.

    The context and page stay alive between searches: ServicePipe clearance is
    bound to this browser, and a fresh challenge on every query is what got the
    exit rate-limited. The clearance cookies are written back to state/5ka.json
    for the next bot start."""
    with _site_lock("5ka"):
        browser = get_browser("5ka")
        context = _persistent_context(browser, "5ka")
        try:
            deadline = _deadline(FIVEKA_DEADLINE_S)
            page = context.pages[0] if context.pages else context.new_page()
            if not page.url.startswith("https://5ka.ru/"):
                page.goto("https://5ka.ru/", wait_until="domcontentloaded",
                          timeout=_timeout_ms(deadline))
            if _fiveka_blocked(page):
                raise ServiceBlocked("5ka block page")
            store = store or SITES["5ka"].get("store")
            if not store:
                raise ValueError("5ka store code is not configured (config/stores.local.json)")
            url = (f"https://5d.5ka.ru/api/catalog/v3/stores/{store}/search"
                   f"?mode=delivery&include_restrict=true&q={quote(query)}")
            data = None
            polls = 0
            while time.time() < deadline:
                res = _evaluate_retrying(page, FIVEKA_FETCH, url, deadline, 250)
                if res and res["status"] == 200:
                    data = json.loads(res["body"])
                    break
                polls += 1
                if polls % 8 == 0:
                    if _fiveka_blocked(page):
                        raise ServiceBlocked("5ka block page")
                    if polls == 8:  # the saved clearance expired: solve one fresh challenge
                        page.goto("https://5ka.ru/", wait_until="domcontentloaded",
                                  timeout=_timeout_ms(deadline))
                page.wait_for_timeout(250)
            if data is None:
                raise ValueError(
                    f"5ka catalog fetch did not return 200 within {FIVEKA_DEADLINE_S}s")
            products = _normalize_5ka(data)
            result = {"products": products, "site": "5ka", "sites": ["5ka"],
                      "site_urls": {"5ka": "https://5ka.ru/search?term=" + quote(query)},
                      "checked": _now_msk(), "url": "https://5ka.ru/search?term=" + quote(query),
                      "corrected": "", "address": None}
            try:
                context.storage_state(path=_state_path("5ka"))
            except Exception:
                event("browser_state_save_failed", site="5ka")
            event("browser_search", site="5ka", candidates=len(products))
            return result
        except Exception:
            # Never keep a blocked or challenged page: the next search (after the
            # cooldown) must start from a clean context, not replay the failure.
            _drop_context("5ka")
            raise


def _normalize_5ka(data):
    """5ka prices/stock arrive as strings ("34.99", "27.00"), rubles not kopecks."""
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ValueError("Unexpected 5ka search response")
    products = []
    for item in data["products"][:36]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            continue
        prices = item.get("prices") or {}
        card_price = _number(prices.get("cpd_promo_price"))
        price = card_price
        if price is None:
            price = _number(prices.get("discount"))
        if price is None:
            price = _number(prices.get("regular"))
        regular = _number(prices.get("regular"))
        old = regular if regular is not None and regular != price else None
        quantity = _number(item.get("stock_limit"))
        if item.get("is_available") is False:
            quantity = 0
        products.append({
            "name": item["name"].strip()[:300],
            "site": "5ka",
            "store": "Пятёрочка",
            "id": str(item["plu"]) if item.get("plu") is not None else None,
            "price": f"{price:.2f}" if price is not None and price >= 0 else None,
            "old_price": f"{old:.2f}" if old is not None and old != price else None,
            "discount": None,
            "rating": None,
            "quantity": quantity,
            "card": card_price is not None,
        })
    return products


def _number(value):
    """5ka sends numbers as strings ("34.99", "27.00"); tolerate both."""
    if type(value) in (int, float):
        return value
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None
    return None


LENTA_STORES = "https://lenta.com/api-gateway/v1/stores/pickup/search"
LENTA_FETCH = """async (args) => {
    const r = await fetch(args.url, {
        method: "POST", credentials: "include", headers: args.headers,
        body: JSON.stringify(args.body)});
    return {status: r.status, body: await r.text()};
}"""


class LentaError(ValueError):
    """Gateway failure; only the HTTP status is safe to log, never the body."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.http_status = status


class LentaStoreMoved(LentaError):
    """The configured delivery store id is gone; its catalogue alias may survive."""


_lenta_store_ids = {}  # normalized catalogue alias -> current delivery store id


def _gateway_headers(token, device, domain):
    """Headers the Angular SPA sends to /api-gateway/v1/* (../shopping/examples/lenta_search.py)."""
    return {"Accept": "application/json", "Content-Type": "application/json",
            "SessionToken": token, "DeviceID": device, "X-Device-ID": device,
            "X-Retail-Brand": "lo", "X-Platform": "omniweb",
            "X-Device-Web-Platform": "desktop_web", "X-Device-OS": "Web",
            "X-Device-OS-Version": "12.4.8", "X-Delivery-Mode": "pickup",
            "X-Domain": domain, "X-Query-Host": "lenta.com",
            "App-Version": "0.0.2", "Client": "angular_web_0.0.2"}


def _lenta_search(page, token, device, domain, query, limit=36, deadline=None):
    payload = {"query": query, "filters": {"checkbox": [], "multicheckbox": [], "range": []},
               "sort": {"type": "popular", "order": "desc"}, "limit": limit, "offset": 0}
    res = _evaluate_retrying(page, LENTA_FETCH,
                             {"url": "https://lenta.com/api-gateway/v1/catalog/items",
                              "headers": _gateway_headers(token, device, domain),
                              "body": payload}, deadline or _deadline(15), 800)
    if not res or res["status"] != 200:
        raise LentaError(f"Lenta search HTTP {res['status'] if res else 'no response'}",
                         res["status"] if res else None)
    return json.loads(res["body"])


def lenta_browser_search(query, proxy=None):
    """Lenta via the in-page gateway call; mirrors ../shopping/examples/lenta_search.py:
    the search doubles as the Qrator readiness probe, then the session is pinned
    to the configured pickup store before the final search."""
    with _site_lock("lenta"):
        browser = get_browser("lenta")
        context = _context(browser, "lenta")
        try:
            deadline = _deadline(30)
            page = context.new_page()
            page.goto("https://lenta.com/search/" + quote(query) + "/",
                      wait_until="domcontentloaded", timeout=_timeout_ms(deadline))
            data = token = device = domain = None
            while time.time() < deadline:
                cookies = {c["name"]: c["value"] for c in context.cookies("https://lenta.com")}
                token = cookies.get("Utk_SessionToken")
                if token:
                    device = cookies.get("Utk_DvcGuid", "")
                    domain = cookies.get("App_Cache_CitySlug", "moscow")
                    try:
                        data = _lenta_search(page, token, device, domain, query,
                                             deadline=deadline)
                        break
                    except ValueError:
                        page.wait_for_timeout(400)
                else:
                    page.wait_for_timeout(400)
            if data is None:
                raise ValueError("Lenta search never succeeded (challenge or block)")
            store_id = _lenta_store_id()
            try:
                token = _lenta_set_store(page, token, device, domain, store_id,
                                         deadline=deadline)
            except LentaStoreMoved:
                # Lenta renumbers pickup stores while catalogue aliases stay put;
                # resolve the current id once and cache it for this process.
                store_id = _lenta_store_by_alias(
                    page, token, device, domain, SITES["lenta"].get("alias"),
                    deadline=deadline)
                event("lenta_store_resolved", alias=SITES["lenta"].get("alias"),
                      store=store_id)
                token = _lenta_set_store(page, token, device, domain, store_id,
                                         deadline=deadline)
            data = _lenta_search(page, token, device, domain, query, deadline=deadline)
            _verify_lenta_store(data)
            products = _normalize_lenta(data)
            result = {"products": products, "site": "lenta", "sites": ["lenta"],
                      "site_urls": {"lenta": "https://lenta.com/search/?query=" + quote(query)},
                      "checked": _now_msk(), "url": "https://lenta.com/search/?query=" + quote(query),
                      "corrected": "", "address": None}
            event("browser_search", site="lenta", candidates=len(products),
                  store=data.get("items", [{}])[0].get("storeId") if data.get("items") else None)
            return result
        finally:
            context.close()


def _lenta_alias_key(value):
    return str(value if value is not None else "").lstrip("0")


def _lenta_store_id():
    alias = _lenta_alias_key(SITES["lenta"].get("alias"))
    if alias and alias in _lenta_store_ids:
        return _lenta_store_ids[alias]
    value = SITES["lenta"].get("store")
    if not value:
        raise ValueError("Lenta store id is not configured (config/stores.local.json)")
    return int(value) if str(value).isdigit() else value


def _lenta_set_store(page, token, device, domain, store_id, deadline=None):
    """Pin the session to the pickup store; returns the (possibly new) token."""
    res = _evaluate_retrying(page, LENTA_FETCH,
                             {"url": "https://lenta.com/api-gateway/v1/delivery/mode/set",
                              "headers": _gateway_headers(token, device, domain),
                              "body": {"type": "pickup", "storeId": store_id}},
                             deadline or _deadline(15), 800)
    if not res or res["status"] != 200:
        status = res["status"] if res else None
        body = (res or {}).get("body") or ""
        if status == 409 and "not found" in body.lower():
            # Lenta renumbers pickup stores; the alias survives. The API words it
            # as "Pickup store with id N not found", so match the tail, not the
            # exact phrase.
            raise LentaStoreMoved(f"Lenta store switch HTTP {status} (store not found)",
                                  status)
        raise LentaError(f"Lenta store switch HTTP {status or 'no response'}", status)
    body = json.loads(res["body"])
    if body.get("storeId") not in (None, store_id):
        raise LentaError("Lenta store switch returned the wrong store")
    return body.get("sessionToken") or token


def _lenta_store_by_alias(page, token, device, domain, alias, deadline=None):
    """Find the current delivery store id for a catalogue alias."""
    key = _lenta_alias_key(alias)
    if not key:
        raise ValueError("Lenta store alias is not configured (config/stores.local.json)")
    res = _evaluate_retrying(page, LENTA_FETCH,
                             {"url": LENTA_STORES,
                              "headers": _gateway_headers(token, device, domain),
                              "body": {}}, deadline or _deadline(15), 800)
    if not res or res["status"] != 200:
        raise LentaError(f"Lenta store lookup HTTP {res['status'] if res else 'no response'}",
                         res["status"] if res else None)
    body = json.loads(res["body"])
    items = body.get("items") if isinstance(body, dict) else None
    for store in items if isinstance(items, list) else []:
        if not isinstance(store, dict) or _lenta_alias_key(store.get("alias")) != key:
            continue
        store_id = store.get("id")
        if store_id is not None:
            _lenta_store_ids[key] = store_id
            return store_id
    raise LentaError("Lenta pickup store alias not found")


def _verify_lenta_store(data):
    """The configured delivery id is exposed as a catalogue alias after selection."""
    alias = SITES["lenta"].get("alias")
    if not alias:
        raise ValueError("Lenta store alias is not configured (config/stores.local.json)")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("Unexpected Lenta search response")
    aliases = {str(item.get("storeId")) for item in items if isinstance(item, dict)
               and item.get("storeId") is not None}
    if aliases and aliases != {str(alias)}:
        raise ValueError("Lenta catalogue returned the wrong store")


def _normalize_lenta(data):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Unexpected Lenta search response")
    products = []
    for item in data["items"][:36]:
        if not isinstance(item, dict):
            continue
        display = item.get("display") or {}
        name = display.get("name") or item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        package = display.get("package")
        if isinstance(package, str) and package.strip() and package.casefold() not in name.casefold():
            name = f"{name.strip()} {package.strip()}"
        prices = item.get("prices") or {}
        price = prices.get("price")
        regular = prices.get("priceRegular")
        products.append({
            "name": name.strip()[:300],
            "site": "lenta",
            "store": "Лента",
            "id": str(item["id"]) if item.get("id") is not None else None,
            "price": f"{price / 100:.2f}" if type(price) in (int, float) and price >= 0 else None,
            "old_price": f"{regular / 100:.2f}"
                         if type(regular) in (int, float) and regular >= 0 and regular != price else None,
            "discount": None,
            "rating": (item.get("rating") or {}).get("rate")
                      if type((item.get("rating") or {}).get("rate")) in (int, float) else None,
            "quantity": item.get("count") if type(item.get("count")) in (int, float) else None,
            "card": bool(prices.get("isLoyaltyCardPrice")),
        })
    return products


BROWSER_SEARCH = {"5ka": fiveka_browser_search, "lenta": lenta_browser_search}


def _now_msk():
    return datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК")
