"""Async boundary for synchronous HTTP and browser store lookups."""
import asyncio
import contextvars
import time
from collections.abc import Callable
from typing import TypeVar

from .store_config import SITES

T = TypeVar("T")


async def run_store(site: str, call: Callable[[], T], *, timeout: float,
                    browser_grace: float = 0) -> T:
    """Run on the appropriate worker, preserving request context.

    The timeout includes queue time. Browser work receives its own deadline;
    callers can allow a short grace period for it to return. Cancellation removes
    queued browser jobs, but cannot stop synchronous work already running.
    Retry and cooldown policy belong to the caller.
    """
    if timeout <= 0:
        raise TimeoutError("store lookup deadline exceeded")
    if not SITES[site].get("browser"):
        return await asyncio.wait_for(asyncio.to_thread(call), timeout)

    from .browser import BROWSER_DEADLINE, site_executor
    context = contextvars.copy_context()
    context.run(BROWSER_DEADLINE.set, time.time() + timeout)
    future = site_executor(site).submit(context.run, call)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout + browser_grace)
    finally:
        # Cancel synchronously as well as via wrap_future's scheduled callback,
        # so a queued job cannot start between cancellation and the next loop turn.
        future.cancel()
