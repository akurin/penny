import contextvars
import asyncio
import os
import unittest
from unittest.mock import patch
from penny.logging_setup import content, failure, redact, request_id


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    def test_content_disabled_by_default(self):
        with patch.dict(os.environ, {"PENNY_LOG_CONTENT": "0"}), patch("penny.logging_setup.event") as emit:
            content("user_message", "private shopping request")
            emit.assert_not_called()

    def test_secrets_and_proxy_credentials_redacted(self):
        with patch.dict(os.environ, {"MODEL_API_KEY": "test-secret"}):
            self.assertEqual(redact("test-secret socks5://user:pass@host:1080"), "[REDACTED] [URL]")

    def test_failure_excludes_exception_message(self):
        with self.assertLogs("penny", level="ERROR") as captured:
            try:
                raise ValueError("super-secret request body")
            except ValueError as exc:
                failure("store", exc)
        output = " ".join(captured.output)
        self.assertNotIn("super-secret", output)
        self.assertIn('"function": "test_failure_excludes_exception_message"', output)

    def test_failure_logs_safe_http_status_without_the_message(self):
        class GatewayError(ValueError):
            http_status = 409

        with self.assertLogs("penny", level="ERROR") as captured:
            try:
                raise GatewayError("private response body")
            except GatewayError as exc:
                failure("store", exc)
        output = " ".join(captured.output)
        self.assertIn("http_status=409", output)
        self.assertNotIn("private response body", output)

    async def test_context_isolated_and_passed_to_worker(self):
        async def worker(identity):
            token = request_id.set(identity)
            try:
                await asyncio.sleep(0)
                return contextvars.copy_context().run(request_id.get)
            finally:
                request_id.reset(token)
        self.assertEqual(await asyncio.gather(worker("one"), worker("two")), ["one", "two"])
