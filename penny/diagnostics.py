"""Safe error summaries: never include exception messages, URLs, or bodies."""
import os
from dotenv import load_dotenv
from .store_config import DEFAULT_SITES, SITES
from .store import (search, StateError, CaptchaBlocked,
                    ensure_ca_bundle)


def summary(stage, exc):
    status = getattr(exc, "http_status", None)
    if type(status) is not int:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    code = getattr(exc, "code", None)
    fields = [f"stage={stage}", f"type={type(exc).__name__}"]
    if type(status) is int:
        fields.append(f"http_status={status}")
    if type(code) is int:
        fields.append(f"transport_code={code}")
    return " ".join(fields)


def main():
    load_dotenv()
    from .store_config import load_store_config
    configured = load_store_config(os.getenv("PENNY_STORES_CONFIG") or None)
    print(f"stores config: {'loaded' if configured else 'missing (stores fail closed)'}")
    ensure_ca_bundle()
    proxy = os.getenv("SOCKS5_PROXY")
    if not proxy:
        raise SystemExit("SOCKS5_PROXY is missing")
    failures = 0
    try:
        for site in DEFAULT_SITES:
            try:
                if SITES[site].get("browser"):
                    from .browser import run_browser_task
                    result = run_browser_task(search, site, "зубная нить Splat", proxy,
                                              site=site)
                else:
                    result = search(site, "зубная нить Splat", proxy)
            except StateError as exc:
                reason = ("captcha cooldown" if "капча" in str(exc)
                          else "stale or missing state file")
                print(f"{site}: {reason} — refresh state/{site}.json via the ../shopping login flow")
                failures += 1
                continue
            except CaptchaBlocked:
                print(f"{site}: exit IP flagged by SmartCaptcha — "
                      "run `python -m penny.dixy_unblock` on a machine with a display")
                failures += 1
                continue
            except Exception as exc:
                print(f"{site}: {summary('store', exc)}")
                if getattr(exc, "code", None) == 7:
                    print("Cannot connect. Check the proxy listener and its address from this OS.")
                failures += 1
                continue
            print(f"{site} OK: {len(result['products'])} candidates")
    finally:
        # Each browser lives on its own mode thread; close them where they run.
        from .browser import close_all_browsers
        close_all_browsers()
    print("No Telegram messages or DeepSeek requests were sent.")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
