"""At most three query rounds; every round checks all configured stores."""
import asyncio
import functools
import os
import time
from .store_config import SITES, DEFAULT_SITES
from .store_execution import run_store
from .store import (search, CaptchaBlocked, ServiceBlocked,
                    SERVICE_BLOCK_COOLDOWN_S, StateError, site_cooldown_active,
                    site_cooldown_remaining, pause_site,
                    note_site_failure, note_site_success)
from .logging_setup import event, content, failure

SITE_NAMES = {site: cfg["name"] for site, cfg in SITES.items()}
STORE_DEADLINE_S = float(os.environ.get("PENNY_STORE_DEADLINE", "30"))


def _lookup_site(lookup, site, q, proxy, sort):
    """Run one lookup; HTTP transports get one connect retry."""
    try:
        return lookup(site, q, proxy, sort)
    except Exception as exc:
        name = type(exc).__name__
        if "Connect" not in name and "ReadError" not in name:
            raise
        event("store_http", site=site, operation="connect_retry", error=name)
        time.sleep(2)
        return lookup(site, q, proxy, sort)


def note_for(site, exc):
    if isinstance(exc, ServiceBlocked):
        return (f"{SITE_NAMES[site]}: сайт временно блокирует запросы с этого "
                "подключения — результаты неполные.")
    if isinstance(exc, TimeoutError):
        return (f"{SITE_NAMES[site]}: не успела проверить за отведённое время — "
                "результаты неполные.")
    if isinstance(exc, StateError):
        if "капча" in str(exc):
            return f"{SITE_NAMES[site]}: {exc}."
        if site == "dixy":
            # The bot creates this session itself when a human solves the captcha;
            # pointing at another project's login flow sent people the wrong way.
            return f"{SITE_NAMES[site]}: нет активной сессии — команда /dixy_captcha её создаст."
        return f"{SITE_NAMES[site]}: {exc}."
    return f"{SITE_NAMES[site]}: проверка не удалась — результаты могут быть неполными."


def cooldown_note(site):
    """A skipped store must say why instead of silently disappearing."""
    minutes = site_cooldown_remaining(site)
    if site == "dixy":
        return f"{SITE_NAMES[site]}: капча — пауза {minutes} мин."
    return (f"{SITE_NAMES[site]}: пауза после сбоев ещё {minutes} мин. — "
            "результаты могут быть неполными.")


def merge(rounds):
    """rounds: per-query dicts {site: result}. Products keep their store; identity is (site, id).
    Within a round products are interleaved across stores, so the candidate cap keeps both."""
    products, seen, sites, urls, counts = [], set(), [], {}, {}
    for ok in rounds:
        per_site = {site: [] for site in DEFAULT_SITES}
        for site in DEFAULT_SITES:
            result = ok.get(site)
            if result is None:
                continue
            if site not in urls:
                sites.append(site)
                urls[site] = result["url"]
            for product in result["products"]:
                identity = (site, product.get("id") or product["name"].casefold())
                if identity in seen:
                    continue
                seen.add(identity)
                per_site[site].append({**product, "checked": result["checked"], "source": result["url"]})
        for index in range(max(len(v) for v in per_site.values()) or 0):
            for site in DEFAULT_SITES:
                if index < len(per_site[site]):
                    products.append(per_site[site][index])
    for site in DEFAULT_SITES:
        count = sum(1 for p in products if p.get("site") == site)
        if count:
            counts[SITE_NAMES[site]] = count
    return {"products": products, "sites": sites, "site_urls": urls, "store_counts": counts,
            "searches": len(rounds) * len(DEFAULT_SITES)}


async def research(model, query, original_request, proxy, lookup=None, sort="popularity"):
    lookup = lookup or search
    notes = []
    incomplete = False
    started = time.monotonic()
    captcha_sites = set()  # stores a human can unblock (Дикси)

    async def check_round(q, sites=DEFAULT_SITES):
        cooling = [s for s in sites if site_cooldown_active(s)]
        if cooling:
            notes.extend(cooldown_note(site) for site in cooling)
            captcha_sites.update(s for s in cooling if s == "dixy")
        sites = tuple(s for s in sites if s not in cooling)
        if not sites:
            return {}
        remaining = STORE_DEADLINE_S - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("store research deadline exceeded")
        tasks = {}
        for site in sites:
            call = functools.partial(_lookup_site, lookup, site, q, proxy, sort)
            tasks[site] = asyncio.create_task(run_store(site, call, timeout=remaining))
        # One wall-clock budget for initial lookups and all follow-up rounds.
        try:
            done, pending = await asyncio.wait(tasks.values(), timeout=remaining)
        finally:
            # This also runs when the user request is cancelled. Retrieve every
            # outcome so failed or cancelled children do not outlive the round.
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        ok, failures = {}, []
        for site, task in tasks.items():
            if task in done:
                try:
                    ok[site] = task.result()
                    note_site_success(site)
                except Exception as exc:
                    failures.append((site, exc))
            else:
                failures.append((site, TimeoutError(f"{site} exceeded the round budget")))
        for site, exc in failures:
            failure(f"store_{site}", exc)
            if isinstance(exc, TimeoutError):
                # Our own wall-clock budget, not the store's health: a cooldown
                # here would hide a working store for ten minutes.
                pass
            elif isinstance(exc, ServiceBlocked):
                # A WAF block page means the exit, not the query; don't retry soon.
                pause_site(site, SERVICE_BLOCK_COOLDOWN_S)
            else:
                note_site_failure(site)
            notes.append(note_for(site, exc))
            if isinstance(exc, CaptchaBlocked):
                captcha_sites.add(site)
        if not ok:
            raise next(iter((exc for _, exc in failures))) from None
        return ok

    results = [await check_round(query)]
    attempted_queries = 1
    primary_ok = set(results[0].keys())
    primary_seconds = time.monotonic() - started
    time_exhausted = False
    # Follow-ups only re-query the fast HTTP stores, so they need a few seconds,
    # not a fixed 20s guard: that guard cut them off whenever a browser store was
    # slow, even with most of the budget still unused.
    slow_primary = primary_seconds > STORE_DEADLINE_S - 10.0
    try:
        first_products = []
        for site in DEFAULT_SITES:
            first_products.extend(results[0].get(site, {}).get("products", []))
        stores = {SITE_NAMES[site]: len(results[0].get(site, {}).get("products", [])) for site in DEFAULT_SITES}
        followups = await model.followups(query, original_request,
                                          {"products": first_products[:30], "stores": stores})
    except Exception as exc:
        failure("research_plan", exc)
        followups = []
        incomplete = True
    # Browser stores are slow and serialized; followups refine the HTTP stores.
    browser_sites = {s for s in DEFAULT_SITES if SITES[s].get("browser")}
    for index, followup in enumerate(followups):
        remaining = [s for s in DEFAULT_SITES
                     if s not in browser_sites and s in primary_ok]
        if not remaining or slow_primary or time.monotonic() - started > STORE_DEADLINE_S:
            time_exhausted = True
            event("research_deadline", primary_seconds=round(primary_seconds, 1),
                  skipped=len(followups) - index)
            break
        content("followup_query", followup)
        attempted_queries += 1
        try:
            results.append(await check_round(followup, tuple(remaining)))
        except Exception:
            pass  # already noted in check_round
    result = merge(results)
    result["notes"] = list(dict.fromkeys(notes))
    result["captcha_sites"] = sorted(captcha_sites)
    if time_exhausted:
        result["notes"] = list(result["notes"]) + ["Не хватило времени на уточняющие поиски — результаты могут быть неполными."]
    result["incomplete"] = incomplete or bool(result["notes"])
    result["sort"] = sort
    event("research_completed", queries=attempted_queries, candidates=len(result["products"]),
          incomplete=result["incomplete"], stores=result["sites"],
          seconds=round(time.monotonic() - started, 1))
    return result
