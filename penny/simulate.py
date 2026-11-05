"""Run one request through Penny's pipeline without Telegram.

    kubectl -n penny exec deploy/penny -- /app/.venv/bin/python -m penny.simulate "хлеб"

The environment is inherited from the container's own process (PID 1), so the
proxy, the DeepSeek key and the store config are exactly the bot's; pass
variables on the command line to override. Nothing is sent to Telegram and no
history is written, so agents can exercise intent parsing, the store round, the
selection model and the rendered reply on demand.
"""
import asyncio
import os
import sys

INHERITED = ("STORE_PROXY", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL",
             "PENNY_STORES_CONFIG", "PENNY_STORE_DEADLINE", "PENNY_LOG_LEVEL",
             "PENNY_LOG_CONTENT")


def _inherit_env():
    """Pick up the bot's settings from PID 1 when kubectl exec gave none."""
    if os.getenv("STORE_PROXY") and os.getenv("DEEPSEEK_API_KEY"):
        return
    try:
        with open("/proc/1/environ", "rb") as handle:
            raw = handle.read()
    except OSError:
        return
    for entry in raw.split(b"\0"):
        name, separator, value = entry.partition(b"=")
        if not separator:
            continue
        key = name.decode(errors="replace")
        if key in INHERITED:
            os.environ.setdefault(key, value.decode(errors="replace"))


def _counts(products):
    counts = {}
    for product in products:
        counts[product["site"]] = counts.get(product["site"], 0) + 1
    return " ".join(f"{site}={count}" for site, count in sorted(counts.items())) or "none"


async def run(text):
    from .logging_setup import configure
    from .research import research
    from .service import Model
    from .store_config import load_store_config

    load_store_config()
    configure()
    model = Model(os.environ["DEEPSEEK_API_KEY"], os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
    intent = await model.interpret(text)
    print(f"intent: route={intent.route} query={intent.query!r} "
          f"sort={intent.sort} budget={intent.budget}", flush=True)
    if intent.route != "catalog" or not intent.query:
        print("nothing to search: informational or unsupported request")
        return 0
    result = await research(model, intent.query, intent.request,
                            os.environ["STORE_PROXY"], sort=intent.sort)
    print(f"stores: {_counts(result.get('products') or [])}", flush=True)
    for note in result.get("notes") or []:
        print(f"note: {note}")
    answer, _ = await model.select(intent.query, result,
                                   original_request=intent.request,
                                   budget=intent.budget, with_meta=True)
    print("\n--- reply ---\n" + answer)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not argv[0].strip():
        print('usage: python -m penny.simulate "<request>"', file=sys.stderr)
        return 2
    _inherit_env()
    missing = [name for name in ("STORE_PROXY", "DEEPSEEK_API_KEY") if not os.getenv(name)]
    if missing:
        print(f"missing environment: {', '.join(missing)}", file=sys.stderr)
        return 2
    return asyncio.run(run(argv[0].strip()))


if __name__ == "__main__":
    sys.exit(main())
