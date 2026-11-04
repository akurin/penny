import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import Mock, patch

from penny.browser import BROWSER_DEADLINE
from penny.logging_setup import request_id
from penny.store_execution import run_store


class StoreExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_preserves_context_and_runs_off_loop(self):
        token = request_id.set('http-request')
        self.addCleanup(request_id.reset, token)
        loop_thread = threading.get_ident()
        identity, worker = await run_store(
            'magnit', lambda: (request_id.get(), threading.get_ident()), timeout=2)
        self.assertEqual(identity, 'http-request')
        self.assertNotEqual(worker, loop_thread)

    async def test_browser_uses_owning_thread_with_isolated_deadlines(self):
        token = request_id.set('browser-request')
        self.addCleanup(request_id.reset, token)
        parent_deadline = BROWSER_DEADLINE.get()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch('penny.browser.site_executor', return_value=executor) as select:
                def inspect():
                    return request_id.get(), BROWSER_DEADLINE.get(), threading.get_ident()
                started = time.time()
                first, second = await asyncio.gather(
                    run_store('5ka', inspect, timeout=2),
                    run_store('5ka', inspect, timeout=4))
                self.assertEqual(first[0], 'browser-request')
                self.assertEqual(first[2], second[2])
                self.assertNotEqual(first[2], threading.get_ident())
                self.assertGreaterEqual(first[1], started + 2)
                self.assertGreater(second[1], first[1] + 1)
                self.assertEqual(BROWSER_DEADLINE.get(), parent_deadline)
                select.assert_called_with('5ka')

    async def test_browser_timeout_cancels_queued_job(self):
        queued = Future()
        executor = Mock()
        executor.submit.return_value = queued
        with patch('penny.browser.site_executor', return_value=executor):
            with self.assertRaises(TimeoutError):
                await run_store('5ka', lambda: None, timeout=.01)
        self.assertTrue(queued.cancelled())

    async def test_browser_cancellation_cancels_queued_job(self):
        queued = Future()
        submitted = asyncio.Event()
        executor = Mock()
        def submit(*args):
            submitted.set()
            return queued
        executor.submit.side_effect = submit
        with patch('penny.browser.site_executor', return_value=executor):
            task = asyncio.create_task(run_store('5ka', lambda: None, timeout=2))
            await asyncio.wait_for(submitted.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(queued.cancelled())

    async def test_expired_budget_never_submits_work(self):
        call = Mock()
        with patch('penny.browser.site_executor') as executor:
            for site in ('magnit', '5ka'):
                with self.assertRaises(TimeoutError):
                    await run_store(site, call, timeout=0)
        call.assert_not_called()
        executor.assert_not_called()

    async def test_worker_errors_reach_caller(self):
        def fail():
            raise ValueError('failed')
        with ThreadPoolExecutor(max_workers=1) as executor:
            with patch('penny.browser.site_executor', return_value=executor):
                for site in ('magnit', '5ka'):
                    with self.assertRaisesRegex(ValueError, 'failed'):
                        await run_store(site, fail, timeout=2)
