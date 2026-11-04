"""Keep startup alive through transient Telegram failures; log polling health safely."""
import asyncio
import logging
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramRetryAfter
from .logging_setup import event, failure


async def wait_for_telegram(bot):
    attempt = 0
    while True:
        try:
            await bot.me()  # Populate aiogram's cache before start_polling calls it.
            event('telegram_connected')
            return
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as exc:
            attempt += 1
            delay = min(5 * 2 ** min(attempt - 1, 3), 30)
            if isinstance(exc, TelegramRetryAfter):
                delay = max(delay, exc.retry_after)
            event('telegram_connect_retry', logging.WARNING, attempt=attempt,
                  retry_seconds=delay, error_type=type(exc).__name__)
            await asyncio.sleep(delay)
        except Exception as exc:
            failure('telegram_startup', exc)
            raise SystemExit('Telegram startup failed. See the safe error type in the log.') from None


class PollingHealth:
    def __init__(self):
        self.failed = False
        self.ready = False

    async def __call__(self, make_request, bot, method):
        if type(method).__name__ != 'GetUpdates':
            return await make_request(bot, method)
        try:
            result = await make_request(bot, method)
        except Exception as exc:
            self.failed = True
            event('telegram_poll_failed', logging.WARNING, error_type=type(exc).__name__,
                  hint='Check connectivity to api.telegram.org; ensure only one Penny instance is running.')
            raise
        if self.failed or not self.ready:
            event('telegram_ready', recovered=self.failed)
        self.failed = False
        self.ready = True
        return result
