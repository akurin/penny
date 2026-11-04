import unittest
import os
import tempfile
from unittest.mock import AsyncMock
from penny.store import (normalize, product_line, render, render_sections,
                         load_state, StateError, DEFAULT_SITES)
from penny.service import Model
from penny.bot import allowed, cooldown_remaining, captcha_keyboard


TEST_SITES = {
    "cosmetic": {"address": "Тестоград, Тестовая 1",
                 "verify": ["тестоград", "тестовая"]},
    "magnit": {"address": "Тестоград, Тестовая 2",
               "verify": ["тестоград", "тестовая"]},
    "dixy": {"address": "Тестоград, Тестовая 3",
             "verify": ["тестоград", "д.3"]},
}


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from penny.store import SITES
        self.saved_sites = {site: {key: SITES[site][key] for key in ("address", "verify")}
                            for site in TEST_SITES}
        for site, values in TEST_SITES.items():
            SITES[site].update(values)

    def tearDown(self):
        from penny.store import SITES
        for site, values in self.saved_sites.items():
            SITES[site].update(values)

    def test_access(self):
        self.assertTrue(allowed(1, "private", {1, 2}))
        self.assertFalse(allowed(3, "private", {1, 2}))
        self.assertFalse(allowed(1, "group", {1, 2}))

    def test_price_and_unknown_stock(self):
        result = normalize({"items": [{"name": "Нить", "price": 12345}]}, "нить")
        answer = render(result, [0])
        self.assertIn("123,45 ₽", answer)
        self.assertIn("наличие неизвестно", answer)
        self.assertIn("М.Косметик · Тестоград, Тестовая 1", answer)
        self.assertNotIn("Проверено", answer)

    def test_missing_price_and_zero_stock(self):
        result = normalize({"items": [{"name": "Нить", "quantity": 0}]}, "нить")
        answer = render(result, [0])
        self.assertIn("цена не указана", answer)
        self.assertIn("нет в наличии", answer)

    def test_old_price_is_shown_without_a_label(self):
        result = normalize({"items": [{
            "name": "Пельмени Сибирская коллекция 700 г", "price": 20999,
            "promotion": {"oldPrice": 47999, "discountPercent": 56},
            "badges": [{"text": "Цена по карте"}]}]}, "пельмени")
        line = product_line(result["products"][0])
        self.assertIn("<b>209,99 ₽</b> по карте (479,99 ₽ −56%)", line)
        self.assertNotIn("старая", line)

    def test_previous_price_is_shown_with_direction(self):
        product = normalize({"items": [{"name": "Молоко 1 л", "price": 8999}]},
                            "молоко")["products"][0]
        product["previous_price"] = "99.90"
        self.assertIn("↘ было 99,90 ₽", product_line(product))
        product["previous_price"] = "79.90"
        self.assertIn("↗ было 79,90 ₽", product_line(product))
        product["previous_price"] = "89.99"
        self.assertNotIn("было", product_line(product))

    def test_best_value_advice_treats_display_equal_unit_prices_as_tie(self):
        from penny.store import best_value_advice
        products = [
            {"name": "Масло A 180 г", "price": "179.99", "store": "Магнит",
             "card": False, "quantity": 3},
            {"name": "Масло B 160 г", "price": "159.99", "store": "Пятёрочка",
             "card": False, "quantity": 3},
        ]
        advice = best_value_advice(products)
        self.assertIn("Одинаково выгодно за кг", advice)
        self.assertIn("Масло A", advice)
        self.assertIn("Масло B", advice)

    def test_slim_includes_unit_price_for_the_model(self):
        from penny.service import slim
        rows = slim(normalize({"items": [{"name": "Молоко 1 л", "price": 8999}]},
                              "молоко")["products"])
        self.assertEqual(rows[0]["unit_price"], "89.99 ₽/л")

    def test_bad_response_is_not_empty_results(self):
        with self.assertRaises(ValueError):
            normalize({"error": "blocked"}, "нить")

    def test_dixy_branch_must_match_configured_store(self):
        from penny.store import normalize_dixy, verify_dixy_branch, BranchVerificationError
        card = {"title": "Хлеб", "priceSimple": "59.99",
                "store": {"storeAddress": "000001, Тестоград, Тестовая ул, д.3"}}
        result = normalize_dixy([{"cards": [card]}], "хлеб")
        self.assertIs(verify_dixy_branch(result), result)
        card["store"]["storeAddress"] = "000002, Другоград, Другая ул, д.1"
        with self.assertRaises(BranchVerificationError):
            verify_dixy_branch(normalize_dixy([{"cards": [card]}], "хлеб"))

    def test_dixy_captcha_cooldown_is_centralized(self):
        """Three consecutive Dixy failures pause it; the transport then fails fast."""
        import penny.store as store
        saved = (store._dixy_captcha_until, store._dixy_fail_streak,
                 dict(store._site_cooldown_until))
        try:
            store._dixy_captcha_until = 0.0
            store._dixy_fail_streak = 0
            store._site_cooldown_until.pop("dixy", None)
            for _ in range(store.DIXY_FAIL_LIMIT):
                store.note_site_failure("dixy")
            self.assertTrue(store.site_cooldown_active("dixy"))
            with self.assertRaisesRegex(StateError, "капча"):
                store.dixy_search("хлеб", "socks5h://127.0.0.1:1")
            store.note_site_success("dixy")
        finally:
            store._dixy_captcha_until, store._dixy_fail_streak = 0.0, 0
            store._site_cooldown_until.clear()
            store._site_cooldown_until.update(saved[2])

    async def test_model_cannot_invent_product_index(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({"items": []}, "stop"))
        with self.assertRaises(ValueError):
            await model.select("нить", normalize({"items": [{"name": "Нить"}]}, "нить"))

    async def test_query_is_bounded(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({"mode": "new", "route": "catalog",
            "query": "x" * 121, "sort": "popularity", "budget_change": "clear",
            "budget": None}, "stop"))
        query, sort, budget = await model.query("hello")
        self.assertIsNone(query)
        self.assertEqual(sort, "popularity")
        self.assertIsNone(budget)

    async def test_query_detects_cheapest_intent(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({"mode": "new", "route": "catalog",
            "query": "гель для душа", "sort": "price_asc", "budget_change": "clear",
            "budget": None}, "stop"))
        query, sort, budget = await model.query("самый дешевый гель для душа")
        self.assertEqual(query, "гель для душа")
        self.assertEqual(sort, "price_asc")
        self.assertIsNone(budget)

    async def test_query_detects_priciest_intent(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({"mode": "new", "route": "catalog",
            "query": "гель для душа", "sort": "price_desc", "budget_change": "clear",
            "budget": None}, "stop"))
        query, sort, budget = await model.query("самый дорогой гель для душа")
        self.assertEqual(query, "гель для душа")
        self.assertEqual(sort, "price_desc")

    async def test_query_rejects_bad_sort(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=({"mode": "new", "route": "catalog",
            "query": "гель", "sort": "random", "budget_change": "clear",
            "budget": None}, "stop"))
        query, sort, budget = await model.query("гель")
        self.assertEqual(query, "гель")
        self.assertEqual(sort, "popularity")

    async def test_query_budget_is_validated(self):
        model = Model("fake", "fake")
        for raw, expected in ((200, 200.0), ("300", 300.0), (0, None), (-5, None),
                              ("пятьсот", None), (200001, None), (None, None)):
            model.ask = AsyncMock(return_value=({"mode": "new", "route": "catalog",
                "query": "порошок", "sort": "popularity",
                "budget_change": "set" if raw is not None else "clear", "budget": raw}, "stop"))
            _, _, budget = await model.query("порошок до 200 рублей")
            self.assertEqual(budget, expected)

    def test_diagnostics_do_not_expose_exception_content(self):
        from penny.diagnostics import summary
        import httpx
        response = httpx.Response(401, request=httpx.Request("GET", "https://secret.example/token"))
        exc = httpx.HTTPStatusError("secret credential", request=response.request, response=response)
        self.assertEqual(summary("selection", exc), "stage=selection type=HTTPStatusError http_status=401")

    def test_transport_diagnostic(self):
        from penny.diagnostics import summary
        exc = RuntimeError("proxy password")
        exc.code = 7
        self.assertEqual(summary("store", exc), "stage=store type=RuntimeError transport_code=7")

    def test_cooldown(self):
        self.assertEqual(cooldown_remaining(None, -500), 0)
        self.assertEqual(cooldown_remaining(100, 108), 2)
        self.assertEqual(cooldown_remaining(100, 110), 0)
        self.assertEqual(cooldown_remaining(100, 120), 0)
        self.assertEqual(cooldown_remaining(100, 130, 60), 30)

    def test_captcha_button_appears_only_when_dixy_needs_a_human(self):
        self.assertIsNone(captcha_keyboard({}))
        self.assertIsNone(captcha_keyboard({"captcha_sites": []}))
        markup = captcha_keyboard({"captcha_sites": ["dixy"]})
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.callback_data, "dixy_captcha")
        self.assertIn("капчу", button.text)

    def test_telegram_command_menu_is_valid(self):
        from penny.bot import COMMANDS
        self.assertEqual([command.command for command in COMMANDS],
                         ["start", "help", "watch", "unwatch", "history", "dixy_captcha"])
        for command in COMMANDS:
            self.assertLessEqual(len(command.command), 32)
            self.assertTrue(3 <= len(command.description) <= 256)

    def test_product_keyboard_offers_each_priced_product(self):
        from penny.bot import product_keyboard
        products = [{"name": "Хлеб 500 г", "price": "39.99"},
                    {"name": "Без цены", "price": None},
                    {"name": "Молоко 1 л", "price": "89.99"}]
        markup = product_keyboard(products, "watch")
        buttons = [row[0] for row in markup.inline_keyboard]
        self.assertEqual([button.callback_data for button in buttons],
                         ["watch:1", "watch:3"])
        self.assertIn("1. Хлеб", buttons[0].text)
        self.assertIsNone(product_keyboard([{"name": "x", "price": None}], "watch"))

    def test_product_keyboard_skips_already_watched(self):
        from penny.bot import product_keyboard
        products = [{"site": "magnit", "id": "1", "name": "Стейк", "price": "399.99"},
                    {"site": "magnit", "id": "2", "name": "Эскалоп", "price": "299.99"}]
        markup = product_keyboard(products, "watch", skip={("magnit", "1")})
        self.assertEqual([row[0].callback_data for row in markup.inline_keyboard],
                         ["watch:2"])
        self.assertIsNone(product_keyboard(products, "watch",
                                           skip={("magnit", "1"), ("magnit", "2")}))

    def test_watch_list_and_unwatch_keyboard(self):
        from penny.bot import unwatch_keyboard, watch_list_text
        watches = [{"id": 7, "name": "Хлеб <500>", "store": "Магнит",
                    "target_price": "39.99"}]
        text = watch_list_text(watches)
        self.assertIn("Магнит", text)
        self.assertIn("39,99 ₽", text)
        self.assertIn("&lt;500&gt;", text)
        markup = unwatch_keyboard(watches)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "unwatch:7")

    def test_reset_site_cooldown_reopens_the_store(self):
        import penny.store as store
        saved = (store._dixy_captcha_until, store._dixy_fail_streak,
                 dict(store._site_cooldown_until), dict(store._site_fail_streaks))
        try:
            for _ in range(store.DIXY_FAIL_LIMIT):
                store.note_site_failure("dixy")
            self.assertTrue(store.site_cooldown_active("dixy"))
            store.reset_site_cooldown("dixy")
            self.assertFalse(store.site_cooldown_active("dixy"))
            self.assertEqual(store._dixy_fail_streak, 0)
            self.assertNotIn("dixy", store._site_fail_streaks)
        finally:
            store._dixy_captcha_until, store._dixy_fail_streak = saved[0], saved[1]
            store._site_cooldown_until.clear()
            store._site_cooldown_until.update(saved[2])
            store._site_fail_streaks.clear()
            store._site_fail_streaks.update(saved[3])

    def test_nonmatching_results_are_distinct_from_empty_search(self):
        result = normalize({"items": [{"name": "Зубная нить Oral-B", "price": 29999},
                                      {"name": "Зубная щетка Splat", "price": 18499}]}, "зубная нить Splat")
        answer = render(result, [])
        self.assertNotIn("Магазин вернул товаров: 2", answer)
        self.assertIn("Точного совпадения", answer)
        self.assertNotIn("Зубная нить Oral-B", answer)
        self.assertNotIn("299,99", answer)
        empty = render(normalize({"items": []}, "нить"), [])
        self.assertIn("Магазин не вернул товаров", empty)

    def test_best_value_advice_uses_unit_price(self):
        from penny.store import best_value_advice
        products = [
            {"name": "Хлеб A 470 г", "price": "119.99", "store": "Лента", "card": True, "quantity": 3},
            {"name": "Хлеб B 515 г", "price": "119.99", "store": "Лента", "card": True, "quantity": 3},
            {"name": "Хлеб C 470 г", "price": "139.99", "store": "Пятёрочка", "card": False, "quantity": 3},
        ]
        advice = best_value_advice(products)
        self.assertIn("Выгоднее за кг среди показанных", advice)
        self.assertIn("Хлеб B", advice)
        self.assertIn("232,99 ₽/кг", advice)
        self.assertIn("Лента", advice)

    def test_best_value_advice_falls_back_to_package_price(self):
        from penny.store import best_value_advice
        products = [
            {"name": "Сок 1 л", "price": "89.99", "store": "Магнит", "card": False, "quantity": 2},
            {"name": "Хлеб 500 г", "price": "39.99", "store": "Лента", "card": False, "quantity": 2},
        ]
        advice = best_value_advice(products)
        self.assertIn("Дешевле всего среди показанных", advice)
        self.assertIn("Хлеб", advice)
        self.assertIn("39,99 ₽", advice)

    def test_best_value_advice_needs_two_priced_products(self):
        from penny.store import best_value_advice
        self.assertIsNone(best_value_advice([]))
        self.assertIsNone(best_value_advice([{"name": "Хлеб 500 г", "price": "39.99"}]))
        self.assertIsNone(best_value_advice([{"name": "Хлеб", "price": None},
                                             {"name": "Батон", "price": None}]))

    def test_render_sections_adds_advice_in_sort_direction(self):
        cosmetic = normalize({"items": [{"name": "Хлеб 500 г", "price": 3999, "quantity": 3}]}, "хлеб")
        magnit = normalize({"items": [{"name": "Хлеб 700 г", "price": 5999, "quantity": 3}]},
                           "хлеб", "magnit")
        combined = {"products": cosmetic["products"] + magnit["products"],
                    "sites": ["cosmetic", "magnit"],
                    "site_urls": {"cosmetic": cosmetic["url"], "magnit": magnit["url"]},
                    "sort": "popularity"}
        answer = render_sections(combined, [{"title": "Хлеб", "items": [0, 1], "note": ""}])
        self.assertIn("Выгоднее за кг среди показанных", answer)
        combined["sort"] = "price_desc"
        answer = render_sections(combined, [{"title": "Хлеб", "items": [0, 1], "note": ""}])
        self.assertIn("Дороже за кг среди показанных", answer)

    def test_hallucinated_summary_sentence_is_dropped(self):
        from penny.service import grounded_conclusion
        products = [{"name": "Вино белое Ла Петит 250 мл"},
                    {"name": "Вино Das Ist Рислинг 0,75 л"}]
        text = ("Самое дешёвое вино среди показанных — «Три топора» в «Магните». "
                "Вино представлено в «Магните» и «Дикси».")
        kept = grounded_conclusion(text, products, "вино")
        self.assertNotIn("Три топора", kept)
        self.assertIn("«Магните» и «Дикси»", kept)

    def test_grounded_summary_keeps_a_shown_brand(self):
        from penny.service import grounded_conclusion
        products = [{"name": "Хлеб HARRY'S American sandwich 470 г"}]
        text = "Бренд Harry's — это хлеб; он есть в «Ленте»."
        self.assertEqual(grounded_conclusion(text, products, "харрис"), text)

    def test_magnit_normalize_and_two_store_render(self):
        magnit = normalize({"items": [{"name": "Порошок", "price": 39999, "quantity": 3}]}, "порошок", "magnit")
        self.assertEqual(magnit["products"][0]["store"], "Магнит")
        self.assertEqual(magnit["url"], "https://magnit.ru/search?term=%D0%BF%D0%BE%D1%80%D0%BE%D1%88%D0%BE%D0%BA")
        cosmetic = normalize({"items": [{"name": "Порошок", "price": 39999, "quantity": 3}]}, "порошок")
        combined = {"products": cosmetic["products"] + magnit["products"],
                    "sites": ["cosmetic", "magnit"],
                    "site_urls": {"cosmetic": cosmetic["url"], "magnit": magnit["url"]},
                    "store_counts": {"М.Косметик": 1, "Магнит": 1}}
        answer = render_sections(combined, [{"title": "Порошки", "items": [0, 1], "note": ""}], strict=False)
        self.assertIn("📍 М.Косметик", answer)
        self.assertIn("📍 Магнит", answer)
        self.assertEqual(answer.count('<a href='), 2)
        self.assertIn('href="https://magnit.ru/search', answer)
        self.assertIn("Тестовая 2", answer)
        self.assertIn("Тестовая 1", answer)

    def test_empty_store_loses_its_footer_link(self):
        cosmetic = normalize({"items": [{"name": "Хлеб", "price": 3999, "quantity": 3}]}, "хлеб")
        magnit = normalize({"items": []}, "хлеб", "magnit")
        combined = {"products": cosmetic["products"], "sites": ["cosmetic", "magnit"],
                    "site_urls": {"cosmetic": cosmetic["url"], "magnit": magnit["url"]},
                    "store_counts": {"М.Косметик": 1}}
        answer = render_sections(combined, [{"title": "Хлеб", "items": [0], "note": ""}], strict=False)
        self.assertIn("Тестовая 1", answer)
        self.assertNotIn("Тестовая 2", answer)
        self.assertEqual(answer.count('<a href='), 1)

    def test_state_file_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(StateError):
                load_state(os.path.join(folder, "missing.json"))
            bad = os.path.join(folder, "bad.json")
            with open(bad, "w") as file:
                file.write("not json")
            with self.assertRaises(StateError):
                load_state(bad)
            partial = os.path.join(folder, "partial.json")
            with open(partial, "w") as file:
                file.write('{"cookies": [{"name": "mg_udi", "value": "x"}]}')
            with self.assertRaises(StateError):
                load_state(partial)

    def test_dixy_robot_page_is_recognized(self):
        """The IP-flag page is distinct from the transient 403 the page retries."""
        from penny.store import is_dixy_block_page

        class Response:
            def __init__(self, status, text):
                self.status_code, self.text = status, text

        self.assertTrue(is_dixy_block_page(Response(403, "<title>403</title> Я не робот")))
        self.assertFalse(is_dixy_block_page(Response(403, "Forbidden")))
        self.assertFalse(is_dixy_block_page(Response(200, "Я не робот")))

    def test_default_sites_and_state_path(self):
        self.assertEqual(DEFAULT_SITES, ("cosmetic", "magnit", "dixy", "5ka", "lenta"))
        from penny.store import SITES
        # Dixy runs on the HTTP transport; its captcha chain is not automated.
        self.assertFalse(SITES["dixy"].get("browser"))
        self.assertTrue(SITES["5ka"]["browser"])
        self.assertTrue(SITES["lenta"]["browser"])
        from penny.store import STATE_PATH
        self.assertTrue(STATE_PATH.endswith("magnit.json"))

    def test_ca_bundle_uses_certifi_unless_overridden(self):
        from penny.store import ensure_ca_bundle
        import certifi
        saved = os.environ.pop("CURL_CA_BUNDLE", None)
        try:
            ensure_ca_bundle()
            self.assertEqual(os.environ["CURL_CA_BUNDLE"], certifi.where())
            os.environ["CURL_CA_BUNDLE"] = "/custom.pem"
            ensure_ca_bundle()
            self.assertEqual(os.environ["CURL_CA_BUNDLE"], "/custom.pem")
        finally:
            os.environ.pop("CURL_CA_BUNDLE", None)
            if saved is not None:
                os.environ["CURL_CA_BUNDLE"] = saved

    def test_html_reply_escapes_names_and_splits_safely(self):
        from penny.store import reply_chunks
        result = normalize({'items': [{'name': '<b>Brand</b> & soap', 'price': 10000}]}, 'soap')
        answer = render(result, [0])
        self.assertIn('&lt;b&gt;Brand&lt;/b&gt; &amp; soap', answer)
        self.assertNotIn('Проверено', answer)
        self.assertEqual(answer.count('<a href='), 1)
        self.assertIn('Открыть в магазине</a>', answer)
        chunks = list(reply_chunks('<b>one</b>\n\n<b>two</b>', limit=12))
        self.assertEqual(chunks, ['<b>one</b>', '<b>two</b>'])
