import json
import os
import time
import unittest
from unittest.mock import patch

from penny.browser import (
    BrowserUnavailable,
    LentaError,
    LentaStoreMoved,
    _lenta_set_store,
    _lenta_store_by_alias,
    _normalize_5ka,
    _normalize_lenta,
    _verify_lenta_store,
    lenta_browser_search,
    shared_headless_mode,
    get_browser,
)
from penny.store import LentaBlocked


class Page:
    def __init__(self, result):
        self.result = result

    def evaluate(self, script, arg):
        return self.result

    def wait_for_timeout(self, milliseconds):
        pass


class FlowPage:
    def __init__(self):
        self.closed = False

    def goto(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, milliseconds):
        pass

    def close(self):
        self.closed = True


class FlowContext:
    def __init__(self, cookies):
        self._cookies = cookies
        self.page = FlowPage()

    def cookies(self, url):
        return self._cookies

    def new_page(self):
        return self.page


class BrowserTests(unittest.TestCase):
    def test_failed_browser_start_cleans_up_manager(self):
        class Manager:
            exited = False

            def __enter__(self):
                raise RuntimeError("failed")

            def __exit__(self, *args):
                self.exited = True

        manager = Manager()
        with patch("penny.browser._launch", return_value=manager), \
                patch("penny.browser.event"):
            with self.assertRaises(BrowserUnavailable):
                get_browser("5ka")
        self.assertTrue(manager.exited)

    def test_shared_browser_uses_lenta_compatible_mode(self):
        with patch.dict(os.environ, {}, clear=False), patch("platform.system", return_value="Linux"):
            os.environ.pop("PENNY_BROWSER_HEADLESS", None)
            self.assertEqual(shared_headless_mode(), "virtual")
        with patch.dict(os.environ, {"PENNY_BROWSER_HEADLESS": "false"}):
            self.assertIs(shared_headless_mode(), False)

    def test_5ka_uses_plain_headless_while_lenta_needs_webgl(self):
        from penny.browser import headless_mode
        with patch.dict(os.environ, {}, clear=False), patch("platform.system", return_value="Linux"):
            os.environ.pop("PENNY_BROWSER_HEADLESS", None)
            self.assertIs(headless_mode("5ka"), True)
            self.assertEqual(headless_mode("lenta"), "virtual")

    def test_5ka_normalizes_card_price_and_explicit_unavailability(self):
        products = _normalize_5ka({"products": [{
            "plu": 7,
            "name": "Молоко 1л",
            "prices": {"regular": "99.99", "discount": "89.99", "cpd_promo_price": "79.99"},
            "stock_limit": "4",
        }, {
            "plu": 8,
            "name": "Кефир 1л",
            "prices": {"regular": "70.00"},
            "stock_limit": "4",
            "is_available": False,
        }]})
        self.assertEqual(products[0]["price"], "79.99")
        self.assertEqual(products[0]["old_price"], "99.99")
        self.assertTrue(products[0]["card"])
        self.assertEqual(products[1]["quantity"], 0)

    def test_browser_normalizers_reject_malformed_success_payloads(self):
        with self.assertRaises(ValueError):
            _normalize_5ka({"error": "blocked"})
        with self.assertRaises(ValueError):
            _normalize_lenta({"error": "blocked"})

    def test_fiveka_block_page_is_detected(self):
        from penny.browser import _fiveka_blocked

        class Page:
            def __init__(self, blocked):
                self.blocked = blocked

            def evaluate(self, script):
                return self.blocked

        self.assertTrue(_fiveka_blocked(Page(True)))
        self.assertFalse(_fiveka_blocked(Page(False)))

    def test_persistent_context_is_reused_between_searches(self):
        from penny.browser import _persistent_context, _site_contexts

        class Context:
            pages = []

            def close(self):
                pass

        class Browser:
            def __init__(self):
                self.created = 0

            def new_context(self, **kwargs):
                self.created += 1
                return Context()

        browser = Browser()
        try:
            first = _persistent_context(browser, "5ka")
            second = _persistent_context(browser, "5ka")
            self.assertIs(first, second)
            self.assertEqual(browser.created, 1)
        finally:
            _site_contexts.clear()

    def test_lenta_name_includes_package_and_price_is_kopecks(self):
        product = _normalize_lenta({"items": [{
            "id": 3,
            "name": "Молоко",
            "display": {"package": "1 л"},
            "prices": {"price": 8999, "priceRegular": 9999, "isLoyaltyCardPrice": True},
            "count": 2,
            "rating": {"rate": 4.8},
        }]})[0]
        self.assertEqual(product["name"], "Молоко 1 л")
        self.assertEqual(product["price"], "89.99")
        self.assertEqual(product["old_price"], "99.99")
        self.assertEqual(product["rating"], 4.8)

    def test_lenta_store_switch_must_succeed(self):
        failed = Page({"status": 403, "body": "blocked"})
        with self.assertRaisesRegex(ValueError, "store switch HTTP 403"):
            _lenta_set_store(failed, "old", "device", "example", 1001)
        ok = Page({"status": 200, "body": json.dumps({"storeId": 1001, "sessionToken": "new"})})
        self.assertEqual(_lenta_set_store(ok, "old", "device", "example", 1001), "new")

    def test_lenta_403_is_not_retried_and_resets_the_browser(self):
        context = FlowContext([
            {"name": "Utk_SessionToken", "value": "token"},
            {"name": "Utk_DvcGuid", "value": "device"},
            {"name": "App_Cache_CitySlug", "value": "moscow"},
        ])
        calls = []

        def blocked(*args, **kwargs):
            calls.append(1)
            raise LentaError("blocked", 403)

        with patch("penny.browser.get_browser", return_value=object()), \
                patch("penny.browser._persistent_context", return_value=context), \
                patch("penny.browser._lenta_search", side_effect=blocked), \
                patch("penny.browser._drop_context") as drop, \
                patch("penny.browser._close_one") as close, \
                patch("penny.browser.event"):
            with self.assertRaises(LentaBlocked) as caught:
                lenta_browser_search("хлеб")
        self.assertEqual(caught.exception.http_status, 403)
        self.assertEqual(len(calls), 1)
        drop.assert_called_once_with("lenta")
        close.assert_called_once_with("virtual")
        self.assertTrue(context.page.closed)

    def test_lenta_challenge_timeout_resets_the_browser(self):
        context = FlowContext([])
        with patch("penny.browser.get_browser", return_value=object()), \
                patch("penny.browser._persistent_context", return_value=context), \
                patch("penny.browser._deadline", return_value=time.time() + 0.01), \
                patch("penny.browser._lenta_search") as search, \
                patch("penny.browser._drop_context") as drop, \
                patch("penny.browser._close_one") as close, \
                patch("penny.browser.event"):
            with self.assertRaises(LentaBlocked):
                lenta_browser_search("хлеб")
        search.assert_not_called()
        drop.assert_called_once_with("lenta")
        close.assert_called_once_with("virtual")

    def test_lenta_transient_errors_are_bounded(self):
        context = FlowContext([{"name": "Utk_SessionToken", "value": "token"}])
        calls = []

        def unavailable(*args, **kwargs):
            calls.append(1)
            raise LentaError("upstream", 503)

        with patch("penny.browser.get_browser", return_value=object()), \
                patch("penny.browser._persistent_context", return_value=context), \
                patch("penny.browser._lenta_search", side_effect=unavailable), \
                patch("penny.browser._drop_context"), \
                patch("penny.browser._close_one") as close, \
                patch("penny.browser.event"):
            with self.assertRaises(LentaBlocked):
                lenta_browser_search("хлеб")
        self.assertEqual(len(calls), 3)
        close.assert_called_once_with("virtual")

    def test_close_all_browsers_submits_every_mode_before_waiting(self):
        from penny.browser import close_all_browsers
        calls, events = [], []

        class BusyFuture:
            def result(self, timeout=None):
                raise TimeoutError

        class DoneFuture:
            def result(self, timeout=None):
                return None

        class Executor:
            def __init__(self, future):
                self.future = future

            def submit(self, fn, key):
                calls.append(("submit", key))
                return self.future

            def shutdown(self, **kwargs):
                calls.append(("shutdown", kwargs))

        executors = {"headless": Executor(BusyFuture()), "virtual": Executor(DoneFuture())}
        with patch("penny.browser.BROWSER_EXECUTORS", executors), \
                patch("penny.browser.event", side_effect=lambda name, **fields: events.append(fields)):
            close_all_browsers(timeout=0.05)
        self.assertEqual([value for action, value in calls if action == "submit"],
                         ["headless", "virtual"])
        self.assertIn(("shutdown", {"wait": False, "cancel_futures": True}), calls)
        self.assertEqual([fields["mode"] for fields in events], ["headless"])

    def test_lenta_store_switch_recognizes_a_renumbered_store(self):
        moved = Page({"status": 409, "body": json.dumps(
            {"code": "BAD_REQUEST", "message": "store not found"})})
        with self.assertRaises(LentaStoreMoved) as caught:
            _lenta_set_store(moved, "old", "device", "example", 1001)
        self.assertEqual(caught.exception.http_status, 409)

    def test_lenta_alias_resolves_the_current_delivery_store(self):
        from penny.browser import _lenta_store_ids
        _lenta_store_ids.clear()
        page = Page({"status": 200, "body": json.dumps({"items": [
            {"id": 4899, "alias": "0600"},
            {"id": 644, "alias": "0644"},
        ]})})
        self.assertEqual(
            _lenta_store_by_alias(page, "token", "device", "example", "644"), 644)

    def test_lenta_catalogue_alias_is_verified(self):
        from penny.store import SITES
        saved = SITES["lenta"].get("alias")
        SITES["lenta"]["alias"] = "101"
        try:
            _verify_lenta_store({"items": [{"storeId": 101}, {"storeId": 101}]})
            with self.assertRaisesRegex(ValueError, "wrong store"):
                _verify_lenta_store({"items": [{"storeId": 42}]})
        finally:
            SITES["lenta"]["alias"] = saved


if __name__ == "__main__":
    unittest.main()
