"""Store transports, response normalization, branch verification and cooldowns."""
import json
import os
import re
from datetime import datetime
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo
from .logging_setup import event

# Keep the original public imports working for scripts and integrations.
# Application code imports these responsibilities from their owning modules.
from .store_config import (ROOT_DIR as ROOT_DIR, STORES_CONFIG_PATH as STORES_CONFIG_PATH,
                           SITES, DEFAULT_SITES as DEFAULT_SITES,
                           STORE_CONFIG_LOADED as STORE_CONFIG_LOADED,
                           load_store_config as load_store_config)
from .products import available as available, unit_price as unit_price
from .rendering import (money as money, product_line as product_line,
                        footer as footer, notes_block as notes_block,
                        best_value_advice as best_value_advice,
                        render_sections as render_sections, render as render,
                        reply_chunks as reply_chunks)

STATE_PATH = os.environ.get("MAGNIT_STATE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "magnit.json")
DIXY_STATE_PATH = os.path.join(os.path.dirname(STATE_PATH), "dixy.json")


class BranchVerificationError(ValueError):
    pass


class CaptchaBlocked(ValueError):
    """The proxy exit is flagged; a human must solve Dixy's SmartCaptcha."""


class ServiceBlocked(ValueError):
    """ServicePipe serves a static block page for this proxy exit (5ka)."""


class StateError(RuntimeError):
    """Magnit session cookies are missing or stale; refresh state/magnit.json via the shopping login flow."""
    pass


def ensure_ca_bundle():
    """Point curl_cffi at certifi's CA bundle.

    The curl-impersonate build bundled with curl_cffi can lag behind fresh
    roots: Dixy moved to GlobalSign Root R46 and the macOS build failed
    verification while Linux and macOS system curl passed. certifi ships with
    httpx and contains the root. CURL_CA_BUNDLE set by the user wins."""
    import certifi
    os.environ.setdefault("CURL_CA_BUNDLE", certifi.where())


def load_state(path, required=("__Host-mg_s", "__Secure-mg_r")):
    """Playwright storage_state with the session cookies for one site."""
    try:
        with open(path, encoding="utf-8") as file:
            state = json.load(file)
    except FileNotFoundError:
        raise StateError("Файл cookie не найден: " + os.path.basename(path))
    except (OSError, json.JSONDecodeError):
        raise StateError("Файл cookie не читается: " + os.path.basename(path))
    cookies = state.get("cookies") if isinstance(state, dict) else None
    if not isinstance(cookies, list):
        raise StateError("В cookie-файле нет cookies")
    named = {c["name"]: c.get("value") for c in cookies
             if isinstance(c, dict) and isinstance(c.get("name"), str)}
    if any(name not in named or not named[name] for name in required):
        raise StateError("В cookie-файле нет обязательных cookie")
    return [c for c in cookies if isinstance(c, dict)
            and isinstance(c.get("name"), str) and isinstance(c.get("value"), str)]


def normalize(data, query, site="cosmetic"):
    cfg = SITES[site]
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("Missing items")
    products = []
    for item in items[:36]:
        price = item.get("price")
        if not isinstance(item.get("name"), str):
            continue
        promo = item.get("promotion") or {}
        old = promo.get("oldPrice")
        discount = promo.get("discountPercent")
        ratings = item.get("ratings") or {}
        products.append({
            "name": item["name"][:300],
            "site": site,
            "store": cfg["name"],
            "id": str(item["id"]) if item.get("id") is not None else None,
            "price": str(Decimal(str(price)) / 100) if type(price) in (int, float) and price >= 0 else None,
            "old_price": str(Decimal(str(old)) / 100) if type(old) in (int, float) and old >= 0 and old != price else None,
            "discount": discount if type(discount) in (int, float) and discount > 0 else None,
            "rating": ratings.get("rating") if type(ratings.get("rating")) in (int, float) else None,
            "reviews": ratings.get("commentsCount") if type(ratings.get("commentsCount")) is int else None,
            "quantity": item.get("quantity") if type(item.get("quantity")) in (int, float) else None,
            "card": any("карт" in str(b.get("text", "")).lower() for b in item.get("badges") or []),
        })
    url = cfg["base"] + "/search?term=" + quote(query)
    return {"products": products, "site": site, "sites": [site], "site_urls": {site: url},
            "checked": datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК"),
            "url": url, "corrected": data.get("correctedTerm") or ""}


DIXY_SEARCH_URL = "https://dixy.ru/catalog/?q={}"


def normalize_dixy(data, query):
    part = next((p for p in data if isinstance(p, dict) and "cards" in p), None)
    if part is None:
        raise ValueError("Unexpected Dixy listing response")
    cards = part.get("cards") or []
    products, address = [], None
    for card in cards[:36]:
        name = card.get("title")
        if not isinstance(name, str) or not name:
            continue
        price = card.get("priceSimple")
        old = card.get("oldPriceSimple")
        if not (isinstance(price, str) or type(price) in (int, float)) or price == "":
            continue
        address = (card.get("store") or {}).get("storeAddress") or address
        try:
            price = Decimal(str(price))
        except Exception:
            continue
        try:
            old = Decimal(str(old)) if old not in (None, "") else None
        except Exception:
            old = None
        # Dixy reports quantity=0 / bskState=["not"] for every card, even clearly
        # stocked bread: it carries no stock data at all. Treat as unknown.
        products.append({
            "name": name[:300],
            "site": "dixy",
            "store": "Дикси",
            "id": None,
            "price": f"{price:.2f}" if price >= 0 else None,
            "old_price": f"{old:.2f}" if old is not None and old != price and old > 0 else None,
            "discount": None,
            "rating": card.get("rating") if type(card.get("rating")) in (int, float) else None,
            "quantity": None,
            "card": False,
        })
    url = SITES["dixy"]["base"] + "/catalog/?q=" + quote(query)
    return {"products": products, "site": "dixy", "sites": ["dixy"], "site_urls": {"dixy": url},
            "checked": datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M МСК"),
            "url": url, "corrected": "", "address": address}


def verify_dixy_branch(result):
    """Never label another branch's prices as the configured store."""
    verify = SITES["dixy"].get("verify") or []
    if not verify:
        raise BranchVerificationError("Dixy branch verification is not configured")
    address = (result.get("address") or "").lower()
    if result.get("products") and not all(token in address for token in verify):
        raise BranchVerificationError("Dixy branch verification failed")
    return result


DIXY_CAPTCHA_COOLDOWN_S = 1800  # SmartCaptcha flags the exit IP; constant pokes
                                # keep the flag alive, so sit out half an hour
SITE_COOLDOWN_S = 600  # any store: 3 consecutive failures -> pause 10 min
SERVICE_BLOCK_COOLDOWN_S = 600  # a WAF block page is not a transient timeout
DIXY_FAIL_LIMIT = 3
_dixy_captcha_until = 0.0
_dixy_fail_streak = 0
_site_fail_streaks = {}
_site_cooldown_until = {}


def site_cooldown_active(site):
    """True while a store sits out its failure cooldown."""
    import time as _time
    return _time.time() < _site_cooldown_until.get(site, 0.0)


def site_cooldown_remaining(site):
    """Whole minutes left in a store's cooldown (0 when it is not cooling)."""
    import time as _time
    remaining = _site_cooldown_until.get(site, 0.0) - _time.time()
    return max(0, int((remaining + 59) // 60))


def note_site_failure(site):
    """Count consecutive failures; pause the store after the limit."""
    import time as _time
    global _dixy_captcha_until, _dixy_fail_streak
    if site == "dixy":
        _dixy_fail_streak += 1
        if _dixy_fail_streak >= DIXY_FAIL_LIMIT:
            _dixy_captcha_until = _time.time() + DIXY_CAPTCHA_COOLDOWN_S
            _dixy_fail_streak = 0
            _site_cooldown_until[site] = _dixy_captcha_until
            event("store_http", site=site, operation="cooldown",
                  minutes=DIXY_CAPTCHA_COOLDOWN_S // 60)
        return
    streak = _site_fail_streaks.get(site, 0) + 1
    _site_fail_streaks[site] = streak
    if streak >= 3:
        _site_fail_streaks[site] = 0
        _site_cooldown_until[site] = _time.time() + SITE_COOLDOWN_S
        event("store_http", site=site, operation="cooldown", minutes=SITE_COOLDOWN_S // 60)


def note_site_success(site):
    global _dixy_fail_streak
    _site_fail_streaks.pop(site, None)
    if site == "dixy":
        _dixy_fail_streak = 0


def reset_site_cooldown(site):
    """A human unblocked the store; let the next lookup try again immediately."""
    global _dixy_captcha_until, _dixy_fail_streak
    _site_cooldown_until.pop(site, None)
    _site_fail_streaks.pop(site, None)
    if site == "dixy":
        _dixy_captcha_until = 0.0
        _dixy_fail_streak = 0


def pause_site(site, seconds):
    """Quiet a store for a while without counting it as a failure."""
    import time as _time
    global _dixy_captcha_until
    until = _time.time() + seconds
    _site_cooldown_until[site] = until
    if site == "dixy":
        _dixy_captcha_until = until


def dixy_search(query, proxy, sort="popularity", state_path=None):
    """Dixy listing AJAX over curl_cffi with state/dixy.json session cookies.

    Failures are counted centrally in note_site_failure (research.check_round);
    this function only respects an already-set cooldown."""
    import time as _time
    until = _site_cooldown_until.get("dixy", 0.0)
    if _time.time() < until:
        minutes = max(1, int((until - _time.time() + 59) // 60))
        raise StateError(f"капча — пауза {minutes} минут")
    from curl_cffi import requests
    path = state_path or DIXY_STATE_PATH
    # Proxy-side DNS (socks5h) and local DNS (socks5) can route through different
    # exits; a banned exit shows up as 403/empty. Try both schemes once each.
    # The Camoufox fallback was removed: Dixy answers a flagged exit with the
    # SmartCaptcha chain checkbox -> slider -> image-text challenge (verified
    # live 2026-10-31), and the final image step is not automatable by design,
    # so the browser only burns the shared time budget. Unblock by hand with
    # penny.dixy_unblock, then the HTTP transport works again.
    schemes = [proxy]
    for a, b in (("socks5h://", "socks5://"), ("socks5://", "socks5h://")):
        if proxy.startswith(a):
            alt = b + proxy[len(a):]
            if alt not in schemes:
                schemes.append(alt)
    last_exc = None
    for scheme in schemes:
        try:
            return _dixy_fetch(requests, path, scheme, query)
        except (BranchVerificationError, StateError, CaptchaBlocked):
            raise
        except Exception as exc:
            last_exc = exc
            if scheme != schemes[-1]:
                event("store_http", site="dixy", operation="dns_fallback")
    raise last_exc


def is_dixy_block_page(response):
    """Dixy's edge block is a 403 HTML page hosting the «Я не робот» SmartCaptcha.

    A plain 403 with a JSON/empty body is the transient flavour the page itself
    retries; the robot page is an exit-IP flag and will not clear on retry."""
    if response.status_code != 403:
        return False
    body = response.text
    return "Я не робот" in body or "<title>403</title>" in body


def _dixy_fetch(requests, path, proxy, query):
    with requests.Session(impersonate="firefox133", proxy=proxy, timeout=20) as session:
        state = load_state(path, required=("PHPSESSID",))
        for cookie in state:
            if "dixy" in cookie.get("domain", ""):
                session.cookies.set(cookie["name"], cookie["value"], domain=".dixy.ru", path="/")
        headers = {"Referer": "https://dixy.ru/catalog/", "Accept": "application/json",
                   "X-Requested-With": "XMLHttpRequest"}
        url = ("https://dixy.ru/ajax/listing-json.php?block=product-list&sid=0"
               "&perPage=30&page=1&searchQuery=" + quote(query) + "&gl_filter=&collect_stat")
        # A fresh session often answers with an empty body (or 403); retry like the page does.
        for attempt in range(3):
            response = session.get(url, headers=headers)
            event("store_http", site="dixy", operation="search", status=response.status_code, attempt=attempt)
            if is_dixy_block_page(response):
                event("store_http", site="dixy", operation="captcha_page")
                raise CaptchaBlocked("captcha: manual unblock via penny.dixy_unblock")
            if response.status_code == 200 and response.text.strip().startswith("["):
                break
            import time
            time.sleep(1.0)
        response.raise_for_status()
        return verify_dixy_branch(normalize_dixy(json.loads(response.text.strip()), query))


def search(site, query, proxy, sort="popularity", state_path=None):
    if SITES[site].get("browser"):
        from .browser import BROWSER_SEARCH
        return BROWSER_SEARCH[site](query, proxy)
    if site == "dixy":
        return dixy_search(query, proxy, sort, state_path)
    cfg = SITES[site]
    if not cfg.get("store"):
        raise StateError(f"{cfg['name']}: store code is not configured (config/stores.local.json)")
    if not cfg.get("verify"):
        raise BranchVerificationError(f"{cfg['name']}: branch verification is not configured")
    from curl_cffi import requests
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Referer": cfg["base"] + "/", "Origin": cfg["base"], "x-client-name": "magnit",
               "x-magnit-bff": "1", "x-new-magnit": "true", "x-permissions-jwt": "true",
               "x-device-platform": "Web", "x-app-version": "0.0.0", "x-platform-version": "Mac Firefox 156"}
    order = ({"order": "asc", "type": "price"} if sort == "price_asc"
             else {"order": "desc", "type": "price"} if sort == "price_desc"
             else {"order": "desc", "type": "popularity"})
    with requests.Session(impersonate="firefox133", proxy=proxy, timeout=20) as session:
        if cfg["cookies"]:
            cookies = load_state(state_path or STATE_PATH)
            for cookie in cookies:
                session.cookies.set(cookie["name"], cookie["value"],
                                    domain=".magnit.ru", path="/")
            named = {c["name"]: c["value"] for c in cookies}
            headers["x-device-id"] = named.get("mg_udi", "")
            headers["x-device-tag"] = named.get("mg_ksid", "") + "_0"
        response = session.post(cfg["base"] + "/webgate/v1/stores-facade/store", headers=headers,
                                json={"externalId": {"owner": "OWNER_MAGNIT", "storeCode": cfg["store"]}})
        event("store_http", site=site, operation="branch", status=response.status_code)
        response.raise_for_status()
        address = str(response.json().get("address", "")).lower()
        if not all(re.search(token, address) for token in cfg["verify"]):
            raise BranchVerificationError("Branch verification failed")
        response = session.post(cfg["base"] + "/webgate/v2/goods/search", headers=headers, json={
            "includeAdultGoods": cfg["adult"], "pagination": {"offset": 0, "limit": 36},
            "sort": order, "term": query,
            "storeCode": cfg["store"], "storeType": "1", "catalogType": "1"})
        event("store_http", site=site, operation="search", status=response.status_code,
              sort=order["type"] + "_" + order["order"])
        response.raise_for_status()
        return normalize(response.json(), query, site)
