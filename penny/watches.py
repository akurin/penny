"""Bounded background price checks for watches; no model calls.

The scheduler walks active watches least-recently-checked first, searches only
the watched product's own store, respects store cooldowns, records the
observation in price history and notifies a drop. It never runs more than
`PENNY_WATCH_LIMIT` checks per pass and never touches DeepSeek.
"""
import asyncio
import functools
import os
from decimal import Decimal, InvalidOperation
from html import escape

from .logging_setup import event
from .memory import Memory
from .store_execution import run_store
from .rendering import money
from .store import (CaptchaBlocked, SERVICE_BLOCK_COOLDOWN_S, ServiceBlocked,
                    note_site_failure, note_site_success, pause_site, search,
                    site_cooldown_active)

DEFAULT_INTERVAL_S = 10800  # three hours
DEFAULT_START_DELAY_S = 300  # let startup settle; no burst right after a restart
DEFAULT_LIMIT = 20
CHECK_TIMEOUT_S = 12  # a browser check must not monopolise a user's round


def find_watch_product(products, watch):
    """Match a search result to the watched product (same site and product key)."""
    for product in products or []:
        if product.get("site") != watch["site"]:
            continue
        if Memory.product_key(product) == watch["product_key"]:
            return product
    return None


async def lookup_site(site, query, proxy, timeout=CHECK_TIMEOUT_S):
    """One store search, on the browser thread when that site needs one."""
    call = functools.partial(search, site, query, proxy)
    return await run_store(site, call, timeout=timeout, browser_grace=5)


async def send_drop_alert(bot, memory, watch, product):
    """One price-drop message; marks an all-time low when it applies."""
    extra = ""
    try:
        stats = memory.price_stats(product.get("site"), Memory.product_key(product))
        if stats and Decimal(str(stats["min"])) == Decimal(str(product["price"])):
            extra = " Это минимум за всё время."
    except (InvalidOperation, TypeError, ValueError):
        pass
    await bot.send_message(
        watch["user_id"],
        f"📉 Цена упала: <b>{escape(watch['name'])}</b> в "
        f"{escape(watch.get('store') or 'магазине')} — было "
        f"{money(watch['target_price'])} ₽, теперь "
        f"<b>{money(product['price'])} ₽</b>.{extra}",
        parse_mode="HTML")


async def check_watches(bot, memory, proxy, lookup=None, limit=None):
    """One bounded pass over active watches; returns {'checked', 'drops'}."""
    lookup = lookup or lookup_site
    limit = limit or int(os.getenv("PENNY_WATCH_LIMIT", str(DEFAULT_LIMIT)))
    try:
        watches = memory.all_watches()
    except Exception as exc:
        event("watch_read_failed", error=type(exc).__name__)
        return {"checked": 0, "drops": 0}
    unique = {}
    for watch in watches:
        unique.setdefault((watch["site"], watch["product_key"]), watch)
    checked = drops = 0
    for watch in list(unique.values())[:max(1, limit)]:
        site = watch["site"]
        if site_cooldown_active(site):
            event("watch_check_skipped", site=site, reason="cooldown")
            continue
        query = (watch.get("name") or "")[:120]
        try:
            result = await lookup(site, query, proxy)
        except ServiceBlocked:
            pause_site(site, SERVICE_BLOCK_COOLDOWN_S)
            event("watch_check_failed", site=site, reason="service_blocked")
            continue
        except CaptchaBlocked:
            note_site_failure(site)
            event("watch_check_failed", site=site, reason="captcha")
            continue
        except Exception as exc:
            note_site_failure(site)
            event("watch_check_failed", site=site, error=type(exc).__name__)
            continue
        note_site_success(site)
        checked += 1
        try:
            memory.touch_check(site, watch["product_key"])
        except Exception as exc:
            event("watch_check_mark_failed", error=type(exc).__name__)
        product = find_watch_product(result.get("products", []), watch)
        if product is None:
            event("watch_not_found", site=site)
            continue
        try:
            memory.record_check(site, product)
        except Exception as exc:
            event("watch_record_failed", error=type(exc).__name__)
        try:
            drops_now = memory.price_drops([product])
        except Exception as exc:
            event("watch_read_failed", error=type(exc).__name__)
            continue
        for active, found in drops_now:
            try:
                await send_drop_alert(bot, memory, active, found)
                memory.mark_drop(active["id"], found["price"])
                drops += 1
            except Exception as exc:
                event("watch_alert_failed", error=type(exc).__name__)
    event("watch_check_completed", watches=len(unique), checked=checked, drops=drops)
    return {"checked": checked, "drops": drops}


async def watch_loop(bot, memory, proxy, interval=None, start_delay=None):
    """Sleep, check, repeat; cancelled when the bot stops."""
    interval = (int(os.getenv("PENNY_WATCH_INTERVAL", str(DEFAULT_INTERVAL_S)))
                if interval is None else interval)
    start_delay = (int(os.getenv("PENNY_WATCH_START_DELAY", str(DEFAULT_START_DELAY_S)))
                   if start_delay is None else start_delay)
    await asyncio.sleep(max(0, start_delay))
    while True:
        try:
            await check_watches(bot, memory, proxy)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event("watch_loop_failed", error=type(exc).__name__)
        await asyncio.sleep(max(60, interval))
