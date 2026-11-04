import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import AsyncMock

from penny.memory import Memory
from penny.service import Model, SearchIntent
from penny.bot import conversation_answer, session_answer


def result(incomplete=False):
    return {
        "products": [{
            "site": "magnit", "store": "Магнит", "id": "42",
            "name": "Молоко 1 л", "price": "99.90", "old_price": None,
            "quantity": 3, "card": False, "checked": "01.01.2026 12:00 МСК",
            "source": "https://magnit.ru/search?term=milk",
        }],
        "sites": ["magnit"],
        "site_urls": {"magnit": "https://magnit.ru/search?term=milk"},
        "notes": ["partial"] if incomplete else [],
        "incomplete": incomplete,
        "sort": "popularity",
    }


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "penny.sqlite3")
        self.memory = Memory(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_session_survives_reopen_and_expires(self):
        shown = [{"name": "Молоко 1 л", "price": "99.90", "store": "Магнит"}]
        self.memory.save_session(7, "молоко до 100 рублей", "молоко", "price_asc", 100,
                                 shown, now=1_000, turn={"route": "catalog"})
        reopened = Memory(self.path)
        session = reopened.get_session(7, max_age=60, now=1_030)
        self.assertEqual(session["query"], "молоко")
        self.assertEqual(session["budget"], 100)
        self.assertEqual(session["products"], shown)
        self.assertEqual(session["turn"], {"route": "catalog"})
        self.assertIsNone(reopened.get_session(7, max_age=60, now=1_061))

    def test_database_file_is_private(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_null_memory_degrades_without_losing_lookups(self):
        from penny.memory import NullMemory
        memory = NullMemory()
        self.assertIsNone(memory.get_session(7))
        self.assertIsNone(memory.cached("молоко", "popularity"))
        memory.save_session(7, "молоко", "молоко", "popularity", None)
        memory.record_search(7, SearchIntent("молоко"), result())
        memory.attach_history(result())
        self.assertEqual(memory.history("magnit", "42"), [])
        self.assertIsNone(memory.price_stats("magnit", "42"))
        memory.save_watch(7, result()["products"][0])
        self.assertEqual(memory.watches(7), [])
        self.assertEqual(memory.all_watches(), [])
        memory.touch_check("magnit", "42")
        self.assertIsNone(memory.record_check("magnit", result()["products"][0]))
        self.assertFalse(memory.remove_watch(7, 1))
        self.assertEqual(memory.price_drops([result()["products"][0]]), [])
        memory.mark_drop(1, "1.00")

    def test_existing_database_is_migrated(self):
        path = os.path.join(self.folder.name, "old.sqlite3")
        with closing(sqlite3.connect(path)) as db:
            db.execute("""CREATE TABLE sessions (
                user_id INTEGER PRIMARY KEY, request TEXT NOT NULL, query TEXT NOT NULL,
                sort TEXT NOT NULL, budget REAL, updated_at INTEGER NOT NULL)""")
            db.commit()
        Memory(path)
        with closing(sqlite3.connect(path)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        self.assertIn("products_json", columns)
        self.assertIn("turn_json", columns)

    def test_complete_result_is_cached_with_observations(self):
        intent = SearchIntent("молоко", request="молоко", continued=True)
        search_id = self.memory.record_search(7, intent, result(), now=2_000)
        cached = self.memory.cached("МОЛОКО", "popularity", max_age=600, now=2_100)
        self.assertEqual(cached["products"][0]["price"], "99.90")
        self.assertEqual(cached["cached_at"], 2_000)
        with closing(sqlite3.connect(self.path)) as db:
            observation = db.execute(
                "SELECT product_key, price FROM observations WHERE search_id = ?",
                (search_id,),
            ).fetchone()
        self.assertEqual(observation, ("42", "99.90"))

    def test_incomplete_result_is_history_but_not_cache(self):
        intent = SearchIntent("молоко", request="молоко")
        self.memory.record_search(7, intent, result(incomplete=True), now=2_000)
        self.assertIsNone(self.memory.cached("молоко", "popularity", now=2_100))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM searches").fetchone()[0], 1)

    def test_cache_expires(self):
        intent = SearchIntent("молоко", request="молоко")
        self.memory.record_search(7, intent, result(), now=2_000)
        self.assertIsNone(self.memory.cached("молоко", "popularity", max_age=600,
                                             now=2_601))

    def test_previous_price_is_attached_before_recording(self):
        intent = SearchIntent("молоко", request="молоко")
        self.memory.record_search(7, intent, result(), now=1_000)
        second = result()
        second["products"][0]["price"] = "89.90"
        self.memory.attach_history(second)
        self.assertEqual(second["products"][0]["previous_price"], "99.90")
        self.memory.record_search(7, intent, second, now=2_000)
        third = result()
        third["products"][0]["price"] = "79.90"
        self.memory.attach_history(third)
        self.assertEqual(third["products"][0]["previous_price"], "89.90")

    def test_history_lists_recent_observations(self):
        intent = SearchIntent("молоко", request="молоко")
        for price in ("99.90", "95.00"):
            found = result()
            found["products"][0]["price"] = price
            self.memory.record_search(7, intent, found, now=1_000)
        rows = self.memory.history("magnit", "42", limit=5)
        self.assertEqual([row["price"] for row in rows], ["95.00", "99.90"])

    def test_price_stats_reports_all_time_low_and_high(self):
        intent = SearchIntent("молоко", request="молоко")
        for price, stamp in (("105.00", "01.11.2026 10:00 МСК"),
                             ("89.90", "02.11.2026 11:00 МСК"),
                             ("99.90", "03.11.2026 12:00 МСК")):
            found = result()
            found["products"][0]["price"] = price
            found["products"][0]["checked"] = stamp
            self.memory.record_search(7, intent, found, now=1_000)
        stats = self.memory.price_stats("magnit", "42")
        self.assertEqual(stats["observations"], 3)
        self.assertEqual(stats["min"], "89.90")
        self.assertEqual(stats["min_checked"], "02.11.2026 11:00 МСК")
        self.assertEqual(stats["max"], "105.00")
        self.assertEqual(stats["max_checked"], "01.11.2026 10:00 МСК")
        self.assertIsNone(self.memory.price_stats("magnit", "missing"))

    def test_watch_reports_a_lower_price_only_once(self):
        product = result()["products"][0]
        self.memory.save_watch(7, product, now=1_000)
        watches = self.memory.watches(7)
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["target_price"], "99.90")
        cheaper = result()["products"][0]
        cheaper["price"] = "89.90"
        drops = self.memory.price_drops([cheaper])
        self.assertEqual([(watch["user_id"], product["price"]) for watch, product in drops],
                         [(7, "89.90")])
        self.memory.mark_drop(watches[0]["id"], cheaper["price"])
        self.assertEqual(self.memory.price_drops([cheaper]), [])
        self.memory.save_watch(8, result()["products"][0])
        self.assertEqual(len(self.memory.price_drops([cheaper])), 1)
        self.assertTrue(self.memory.remove_watch(8, self.memory.watches(8)[0]["id"]))
        self.assertFalse(self.memory.remove_watch(8, 9999))

    def test_background_check_records_history_but_not_cache(self):
        product = result()["products"][0]
        product["checked"] = "01.11.2026 03:00 МСК"
        self.memory.record_check("magnit", product, now=3_000)
        rows = self.memory.history("magnit", "42")
        self.assertEqual(rows[0]["price"], "99.90")
        self.assertEqual(rows[0]["checked_at"], "01.11.2026 03:00 МСК")
        # background checks are history, never a cache hit for user queries
        self.assertIsNone(self.memory.cached("Молоко 1 л", "popularity"))

    def test_all_watches_are_listed_across_users(self):
        product = result()["products"][0]
        self.memory.save_watch(7, product, now=1_000)
        self.memory.save_watch(8, product, now=1_000)
        watches = self.memory.all_watches()
        self.assertEqual({watch["user_id"] for watch in watches}, {7, 8})
        self.memory.touch_check("magnit", "42", now=2_000)
        self.assertTrue(all(watch["checked_at"] == 2_000
                            for watch in self.memory.all_watches()))


class IntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_average_and_composition_are_session_actions(self):
        model = Model("fake", "fake")
        previous = {"query": "Харрис", "request": "Харрис"}
        cases = (("какая средняя цена?", "results", "none", "price", "average"),
                 ("какой состав?", "external_info", "composition", "none", "none"))
        for message, route, topic, metric, aggregation in cases:
            with self.subTest(message=message):
                model.ask = AsyncMock(return_value=({
                    "mode": "continue", "query": None, "sort": "inherit",
                    "budget_change": "inherit", "budget": None, "refresh": False,
                    "route": route, "info_topic": topic,
                    "result_metric": metric, "result_aggregation": aggregation,
                }, "stop"))
                intent = await model.interpret(message, previous)
                self.assertTrue(intent.continued)
                self.assertEqual(intent.route, route)
                self.assertEqual(intent.info_topic, topic)

    async def test_bare_choose_after_suitability_keeps_the_topic(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "continue", "query": None, "sort": "price_asc",
            "budget_change": "inherit", "budget": None, "refresh": False,
            "route": "results", "result_metric": "product",
            "result_aggregation": "choose",
        }, "stop"))
        previous = {"request": "попкорн\n→ а что лучше для ребенка?", "query": "попкорн",
                    "sort": "popularity", "budget": None,
                    "turn": {"route": "external_info", "info_topic": "suitability"}}
        intent = await model.interpret("выбери один", previous)
        self.assertEqual(intent.route, "external_info")
        self.assertEqual(intent.info_topic, "suitability")
        self.assertEqual(intent.result_metric, "none")
        self.assertEqual(intent.result_aggregation, "none")

    async def test_standalone_product_composition_is_not_a_price_search(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "new", "query": "хлеб Харрис", "sort": "popularity",
            "budget_change": "clear", "budget": None,
            "route": "external_info", "info_topic": "composition",
        }, "stop"))
        intent = await model.interpret("хлеб харрис, состав", None)
        self.assertEqual(intent.query, "хлеб Харрис")
        self.assertEqual(intent.route, "external_info")
        self.assertEqual(intent.info_topic, "composition")
        self.assertEqual(intent.request, "хлеб Харрис")
        self.assertFalse(intent.continued)

    async def test_fresh_request_for_unsupported_store_is_rejected(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "unsupported", "query": None, "sort": "popularity",
            "budget_change": "clear", "budget": None, "route": "catalog",
        }, "stop"))
        intent = await model.interpret("молоко в Перекрёстке", None)
        self.assertIsNone(intent.query)

    async def test_model_marks_standalone_brand_new(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "new", "query": "Харрис", "request": "Харрис",
            "sort": "popularity", "budget": None, "refresh": False,
        }, "stop"))
        previous = {"query": "кофе", "request": "кофе"}
        intent = await model.interpret("Харрис", previous)
        self.assertFalse(intent.continued)
        self.assertEqual(intent.query, "Харрис")
        self.assertEqual(intent.request, "Харрис")
        self.assertEqual(model.ask.call_args.args[1]["previous"], previous)

    async def test_model_can_continue_previous_intent(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "continue", "query": None, "sort": "price_asc",
            "budget_change": "inherit", "budget": None, "refresh": False,
            "route": "catalog",
        }, "stop"))
        previous = {"request": "зубная нить Splat", "query": "зубная нить Splat",
                    "sort": "popularity", "budget": None}
        intent = await model.interpret("а подешевле?", previous)
        self.assertTrue(intent.continued)
        self.assertEqual(intent.sort, "price_asc")
        self.assertEqual(intent.request, "зубная нить Splat\n→ а подешевле?")
        self.assertEqual(model.ask.call_args.args[1]["previous"], previous)

    async def test_where_is_cheaper_continues_previous_intent(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "continue", "query": None, "sort": "price_asc",
            "budget_change": "inherit", "budget": None, "refresh": False,
            "route": "catalog",
        }, "stop"))
        previous = {"request": "харрис", "query": "харрис",
                    "sort": "popularity", "budget": None}
        intent = await model.interpret("где дешевле?", previous)
        self.assertTrue(intent.continued)
        self.assertEqual(intent.query, "харрис")
        self.assertEqual(intent.sort, "price_asc")

    async def test_complete_request_starts_new_intent(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "new", "query": "молоко", "request": "молоко",
            "sort": "popularity", "budget": None, "refresh": False,
        }, "stop"))
        intent = await model.interpret("молоко", {"query": "хлеб"})
        self.assertFalse(intent.continued)
        self.assertEqual(intent.query, "молоко")

    async def test_refresh_is_a_continuation_and_bypasses_cache_signal(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "mode": "continue", "query": "молоко", "request": "молоко",
            "sort": "popularity", "budget": None, "refresh": True,
        }, "stop"))
        intent = await model.interpret("обнови цены", {"query": "молоко"})
        self.assertTrue(intent.continued)
        self.assertTrue(intent.refresh)

    async def test_unsupported_does_not_inherit_previous(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "query": None, "sort": "popularity", "budget": None,
        }, "stop"))
        intent = await model.interpret("привет", {"query": "молоко"})
        self.assertIsNone(intent.query)
        self.assertFalse(intent.continued)

    async def test_continuation_without_session_fails_closed(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({
            "query": None, "sort": "popularity", "budget": None,
        }, "stop"))
        intent = await model.interpret("а дешевле?", None)
        self.assertIsNone(intent.query)
        self.assertFalse(intent.continued)


class SessionQuestionTests(unittest.TestCase):
    def test_average_is_calculated_by_code(self):
        products = [
            {"name": "A", "price": "129.99", "store": "Магнит"},
            {"name": "B", "price": "143.90", "store": "Дикси"},
            {"name": "C", "price": "139.99", "store": "Пятёрочка"},
            {"name": "D", "price": "119.99", "store": "Лента"},
            {"name": "E", "price": "119.99", "store": "Лента"},
        ]
        intent = SearchIntent("товар", route="results", result_metric="price",
                              result_aggregation="average")
        answer = session_answer(intent, products)
        self.assertIn("130,77 ₽", answer)
        self.assertIn("(5)", answer)

    def test_generic_result_operations(self):
        products = [
            {"name": "A", "price": "120.00", "store": "Магнит"},
            {"name": "B", "price": "150.00", "store": "Лента"},
            {"name": "C", "price": "130.00", "store": "Лента"},
        ]
        cases = [
            ("product", "count", "3"),
            ("store", "count", "2"),
            ("store", "list", "Магнит, Лента"),
            ("price", "minimum", "120,00 ₽"),
            ("price", "maximum", "150,00 ₽"),
            ("price", "list", "120,00 ₽ — A"),
        ]
        for metric, aggregation, expected in cases:
            with self.subTest(metric=metric, aggregation=aggregation):
                intent = SearchIntent("товар", route="results", result_metric=metric,
                                      result_aggregation=aggregation)
                self.assertIn(expected, session_answer(intent, products))

    def test_unit_price_operations_use_package_sizes(self):
        products = [
            {"name": "Попкорн A 85 г", "price": "51.99", "store": "Магнит"},
            {"name": "Попкорн B 65 г", "price": "99.99", "store": "Лента"},
        ]
        cheapest = session_answer(
            SearchIntent("попкорн", route="results", result_metric="unit_price",
                         result_aggregation="minimum"), products)
        listing = session_answer(
            SearchIntent("попкорн", route="results", result_metric="unit_price",
                         result_aggregation="list"), products)
        self.assertIn("611,65 ₽/кг", cheapest)
        self.assertIn("1538,31 ₽/кг", listing)

    def test_choose_without_criterion_asks_for_one(self):
        answer = session_answer(
            SearchIntent("попкорн", route="results", result_metric="product",
                         result_aggregation="choose"),
            [{"name": "Попкорн A", "price": "50", "store": "Магнит"}])
        self.assertIn("По какому критерию", answer)

    def test_composition_is_honest_without_catalogue_noise(self):
        products = [
            {"name": "Хлеб Harry's 470 г", "price": "129.99", "store": "Магнит"},
            {"name": "Хлеб Harry's с отрубями 515 г", "price": "119.99", "store": "Лента"},
        ]
        intent = SearchIntent("хлеб Харрис", route="external_info", info_topic="composition")
        answer = session_answer(intent, products)
        self.assertIn("состав", answer)
        self.assertNotIn("Huggies", answer)
        self.assertNotIn("Harry's", answer)

    def test_composition_without_saved_products_does_not_loop(self):
        intent = SearchIntent("хлеб Харрис", route="external_info", info_topic="composition")
        answer = session_answer(intent, [])
        self.assertIn("состав", answer)
        self.assertNotIn("Напишите название товара ещё раз", answer)

    def test_external_info_topics_do_not_reuse_composition_answer(self):
        origin = session_answer(
            SearchIntent("хлеб Харрис", route="external_info", info_topic="origin"), [])
        general = session_answer(
            SearchIntent("хлеб Харрис", route="external_info", info_topic="general"), [])
        self.assertIn("место производства", origin)
        self.assertNotIn("состав", origin)
        self.assertIn("название, цену", general)

    def test_suitability_does_not_invent_a_health_recommendation(self):
        answer = session_answer(
            SearchIntent("попкорн", route="external_info", info_topic="suitability"), [])
        self.assertIn("нельзя надёжно оценить", answer)
        self.assertIn("возрастной маркировки", answer)


class ConversationAnswerTests(unittest.TestCase):
    def test_acknowledgement_gets_a_short_reply(self):
        self.assertEqual(conversation_answer("acknowledgement"), "Рада помочь!")

    def test_capabilities_describe_real_sources(self):
        answer = conversation_answer("capabilities")
        self.assertIn("каталоги", answer)
        self.assertIn("Внешний источник", answer)
        self.assertNotIn("место производства", answer)

