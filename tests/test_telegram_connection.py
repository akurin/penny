import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe, GetUpdates
from penny.bot import answer_with_retry
from penny.telegram_connection import wait_for_telegram, PollingHealth


class AnswerRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_once_on_telegram_network_error(self):
        answer = AsyncMock(side_effect=[TelegramNetworkError(method=GetMe(), message='x'), object()])
        with patch('penny.bot.asyncio.sleep', new_callable=AsyncMock) as sleep, \
                patch('penny.bot.event') as event:
            await answer_with_retry(answer, "текст")
        self.assertEqual(answer.await_count, 2)
        answer.assert_any_await("текст")
        sleep.assert_awaited_once_with(2)
        self.assertEqual(event.call_args.args[0], "telegram_send_retry")

    async def test_raises_after_last_attempt(self):
        answer = AsyncMock(side_effect=TelegramNetworkError(method=GetMe(), message='x'))
        with patch('penny.bot.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(TelegramNetworkError):
                await answer_with_retry(answer, "текст")
        self.assertEqual(answer.await_count, 2)
        sleep.assert_awaited_once_with(2)

    async def test_non_network_errors_are_not_retried(self):
        answer = AsyncMock(side_effect=ValueError('x'))
        with self.assertRaises(ValueError):
            await answer_with_retry(answer, "текст")
        self.assertEqual(answer.await_count, 1)

    async def test_kwargs_are_forwarded(self):
        answer = AsyncMock(return_value=object())
        await answer_with_retry(answer, "текст", parse_mode="HTML")
        answer.assert_awaited_once_with("текст", parse_mode="HTML")


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_retries_then_connects(self):
        bot = AsyncMock()
        bot.me.side_effect = [TelegramNetworkError(method=GetMe(), message='secret URL'), object()]
        with patch('penny.telegram_connection.asyncio.sleep', new_callable=AsyncMock) as sleep, patch('penny.telegram_connection.event') as event:
            await wait_for_telegram(bot)
        sleep.assert_awaited_once_with(5)
        self.assertEqual(bot.me.await_count, 2)
        self.assertNotIn('secret URL', str(event.call_args_list))

    async def test_bad_token_does_not_retry(self):
        bot = AsyncMock()
        bot.me.side_effect = TelegramUnauthorizedError(method=GetMe(), message='secret')
        with patch('penny.telegram_connection.failure'), patch('penny.telegram_connection.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(SystemExit):
                await wait_for_telegram(bot)
        sleep.assert_not_awaited()

    async def test_cancellation_stops_startup(self):
        bot = AsyncMock()
        bot.me.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await wait_for_telegram(bot)

    async def test_polling_recovery_is_logged(self):
        health = PollingHealth()
        request = AsyncMock(side_effect=[TelegramNetworkError(method=GetUpdates(), message='secret'), []])
        with patch('penny.telegram_connection.event') as event:
            with self.assertRaises(TelegramNetworkError):
                await health(request, object(), GetUpdates())
            self.assertEqual(await health(request, object(), GetUpdates()), [])
        self.assertEqual(event.call_args.args[0], 'telegram_ready')
        self.assertTrue(event.call_args.kwargs['recovered'])
        self.assertNotIn('secret', str(event.call_args_list))
