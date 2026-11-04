import os
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock
from penny.service import Model, sanitize_aligned
from penny.store import normalize, render


def aligned(items, **changes):
    base = {"items": items, "conclusion": "", "strict": False}
    base.update(changes)
    return base


def show(section=True, relation="direct"):
    if section is True:
        return {"show": True, "section": "Подходящее", "relation": relation}
    if section:
        return {"show": True, "section": section, "relation": relation}
    return {"show": False}


def products():
    return normalize({"items": [
        {"name": "Зубная нить Oral-B 50м", "price": 29999, "quantity": 5, "badges": [{"text": "По карте"}]},
        {"name": "Зубочистки с нитью Dental Floss 30шт", "price": 12999},
        {"name": "Зубная щетка Splat", "price": 18499},
        {"name": "Зубная нить Splat", "price": 20000}]}, "нить Splat")


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_display_session_and_watch_buttons_share_order(self):
        from penny.bot import product_keyboard
        from penny.memory import Memory
        for relation in ('direct', 'alternative'):
            for sort, expected in (('popularity', [2, 1, 0]),
                                   ('price_asc', [2, 0, 1]),
                                   ('price_desc', [1, 0, 2])):
                with self.subTest(relation=relation, sort=sort):
                    found = normalize({'items': [
                        {'name': 'Молоко А 1 л', 'price': 20000, 'quantity': 3},
                        {'name': 'Молоко Б 2 л', 'price': 30000, 'quantity': 3},
                        {'name': 'Молоко В 1 л', 'price': 10000, 'quantity': 3},
                    ]}, 'молоко')
                    found['sort'] = sort
                    before = deepcopy(found)
                    model = Model('fake', 'fake')
                    model.ask = AsyncMock(return_value=(aligned([
                        show('Молоко', relation) for _ in range(3)]), 'stop'))
                    answer, primary = await model.select('молоко', found, with_meta=True)
                    self.assertEqual(found, before)
                    self.assertEqual(primary, [found['products'][i] for i in expected])
                    positions = [answer.index(f'<b>{n}. Молоко {"АБВ"[i]}</b>')
                                 for n, i in enumerate(expected, 1)]
                    self.assertEqual(positions, sorted(positions))
                    with tempfile.TemporaryDirectory() as folder:
                        memory = Memory(os.path.join(folder, 'memory.sqlite3'))
                        memory.save_session(1, 'молоко', 'молоко', sort, None, primary)
                        saved = memory.get_session(1)['products']
                    self.assertEqual(saved, primary)
                    keyboard = product_keyboard(saved, 'watch')
                    for n, (row, product) in enumerate(zip(keyboard.inline_keyboard, primary), 1):
                        self.assertEqual(row[0].text, f'{n}. {product["name"]}')
                        self.assertEqual(row[0].callback_data, f'watch:{n}')

    async def test_price_order_keeps_alternatives_out_of_primary_metadata(self):
        model = Model('fake', 'fake')
        model.ask = AsyncMock(return_value=(aligned([
            show(), show('Другой формат', 'different_format'), show(False), show()]), 'stop'))
        found = {**products(), 'sort': 'price_asc'}
        answer, primary = await model.select('нить', found, with_meta=True)
        self.assertEqual(primary, [found['products'][3], found['products'][0]])
        self.assertLess(answer.index('<b>1. Зубная нить Splat</b>'),
                        answer.index('<b>2. Зубная нить Oral-B</b>'))

    async def test_sections_have_grounded_prices(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([
            show(), show("Другой формат"), show(False), show(False)]), "stop"))
        answer = await model.select("нить Splat", products(), "Сколько стоит нить Splat?")
        self.assertIn("Подходящее", answer)
        self.assertIn("<b>299,99 ₽</b> по карте", answer)
        self.assertIn("Другой формат", answer)
        self.assertIn("129,99 ₽", answer)
        self.assertNotIn("Зубная щетка", answer)
        self.assertEqual(model.ask.call_args.args[1]["original_request"], "Сколько стоит нить Splat?")

    async def test_selection_metadata_keeps_only_primary_section(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([
            show("Хлеб Harry's"), show("Похожее", "alternative"),
            show(False), show(False)]), "stop"))
        answer, primary = await model.select(
            "Harry's", products(), "Harry's", with_meta=True)
        self.assertIn("Хлеб Harry&#x27;s", answer)
        self.assertEqual([product["name"] for product in primary], ["Зубная нить Oral-B 50м"])

    async def test_strict_keeps_single_section(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([
            show(False), show(False), show(False), show("Только Splat")], strict=True), "stop"))
        answer = await model.select("нить Splat", products(), "только Splat, без аналогов")
        self.assertNotIn("Oral-B", answer)
        self.assertNotIn("Dental Floss", answer)
        self.assertIn("Зубная нить Splat", answer)

    def test_exact_results_take_priority(self):
        answer = render(products(), [3], [0], [1])
        self.assertIn("Зубная нить Splat", answer)
        self.assertNotIn("Oral-B", answer)
        self.assertNotIn("Dental Floss", answer)

    def test_sanitize_repairs_instead_of_failing(self):
        cleaned = sanitize_aligned(aligned([
            {"show": True, "section": "t"}, {"show": True, "section": ""},
            {"show": True}, {"show": False}, "junk", None]), "stop", 6)
        self.assertEqual(cleaned["sections"], [{"title": "t", "items": [0], "note": ""}])

    def test_sanitize_collapses_whitespace_in_titles(self):
        cleaned = sanitize_aligned(aligned([
            {"show": True, "section": "Порошки\n\nи\n гели"}, {"show": False}]), "stop", 2)
        self.assertEqual(cleaned["sections"][0]["title"], "Порошки и гели")

    def test_sanitize_caps_sections_items_and_total(self):
        items = [{"show": True, "section": f"s{i % 4}"} for i in range(12)]
        cleaned = sanitize_aligned(aligned(items), "stop", 12)
        self.assertEqual(len(cleaned["sections"]), 3)
        for section in cleaned["sections"]:
            self.assertLessEqual(len(section["items"]), 5)
        total = sum(len(s["items"]) for s in cleaned["sections"])
        self.assertLessEqual(total, 9)
        self.assertGreaterEqual(cleaned["dropped"], 3)

    def test_sanitize_fails_closed_when_cut_or_empty(self):
        with self.assertRaises(ValueError):
            sanitize_aligned(aligned([show(), show()]), "length", 2)
        for invalid in (aligned([]), {"items": "nope"}, {"nope": 1}, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sanitize_aligned(invalid, "stop", 2)

    def test_deliberate_all_hidden_is_a_verdict_not_an_error(self):
        cleaned = sanitize_aligned(aligned([show(False), show(False)]), "stop", 2)
        self.assertEqual(cleaned["sections"], [])

    async def test_all_hidden_keeps_the_model_conclusion(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([
            {"show": False}, {"show": False}],
            conclusion="Пищевого тофу нет — только наполнитель и конфеты"), "stop"))
        found = normalize({"items": [{"name": "Наполнитель Tofu", "price": 10000, "quantity": 5},
                                     {"name": "Конфеты Toffifee", "price": 20000, "quantity": 3}]}, "тофу")
        answer = await model.select("тофу", found)
        self.assertIn("Подходящих товаров в найденном нет", answer)
        self.assertIn("Пищевого тофу нет", answer)
        self.assertIn("М.Косметик", answer)
        self.assertNotIn("Наполнитель Tofu", answer)

    async def test_conclusion_is_rendered_as_a_summary(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([
            show(), show("Другое", "alternative"), show(False), show(False)],
            conclusion="Splat нашлась в двух магазинах, Oral-B дороже."), "stop"))
        answer = await model.select("нить Splat", products(), "нить Splat")
        self.assertIn("<i>Splat нашлась в двух магазинах, Oral-B дороже.</i>", answer)

    def test_full_section_swaps_in_the_cheapest_direct_candidate(self):
        from penny.service import sanitize_aligned
        products = [{"name": f"Масло {index} 180 г", "price": "199.99"} for index in range(5)]
        products.append({"name": "Масло дешёвое 180 г", "price": "99.99"})
        items = [{"show": True, "section": "Масло", "relation": "direct"}
                 for _ in products]
        cleaned = sanitize_aligned({"items": items, "strict": False}, "stop",
                                   len(products), products=products)
        shown = cleaned["sections"][0]["items"]
        self.assertEqual(len(shown), 5)
        self.assertIn(5, shown)
        self.assertNotIn(4, shown)

    def test_excluded_cheap_candidate_is_not_forced(self):
        from penny.service import sanitize_aligned
        products = [{"name": f"Масло {index} 180 г", "price": "199.99"} for index in range(5)]
        products.append({"name": "Масло дешёвое 180 г", "price": "99.99"})
        items = [{"show": True, "section": "Масло", "relation": "direct"} for _ in range(5)]
        items.append({"show": False})
        cleaned = sanitize_aligned({"items": items, "strict": False}, "stop",
                                   len(products), products=products)
        self.assertNotIn(5, cleaned["sections"][0]["items"])

    def test_hidden_direct_cheapest_is_forced_into_a_direct_section(self):
        from penny.service import sanitize_aligned
        products = [{"name": f"Масло {index} 180 г", "price": "199.99"} for index in range(5)]
        products.append({"name": "Масло дешёвое 180 г", "price": "99.99"})
        items = [{"show": True, "section": "Масло", "relation": "direct"} for _ in range(5)]
        items.append({"show": False, "section": "Другая", "relation": "direct"})
        cleaned = sanitize_aligned({"items": items, "strict": False}, "stop",
                                   len(products), products=products)
        self.assertIn(5, cleaned["sections"][0]["items"])

    def test_false_friend_guard_rejects_word_traps(self):
        from penny.service import is_false_friend
        self.assertTrue(is_false_friend("Жевательная резинка Pure Fresh Виноград Mentos", "вино"))
        self.assertTrue(is_false_friend("Кофемашина", "кофе"))
        self.assertFalse(is_false_friend("Белое вино безалкогольное Ла Петит", "вино"))
        self.assertFalse(is_false_friend("Вино из винограда", "вино"))
        self.assertFalse(is_false_friend("Вином столовым", "вино"))
        self.assertFalse(is_false_friend("Сырок глазированный", "сыр"))

    async def test_false_friend_is_filtered_before_selection(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([show(False)]), "stop"))
        found = normalize({"items": [
            {"name": "Жевательная резинка Виноград", "price": 3699, "quantity": 5},
            {"name": "Белое вино Ла Петит", "price": 18999, "quantity": 5}]}, "вино")
        await model.select("вино", found)
        sent = model.ask.call_args.args[1]["products"]
        self.assertEqual([p["name"] for p in sent], ["Белое вино Ла Петит"])

    async def test_empty_search_skips_model(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock()
        answer = await model.select("нить", normalize({"items": []}, "нить"))
        model.ask.assert_not_called()
        self.assertIn("не вернул товаров", answer)

    async def test_out_of_stock_hidden_from_model(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([show()]), "stop"))
        found = normalize({"items": [
            {"name": "Порошок 3кг", "price": 44999, "quantity": 0},
            {"name": "Гель 1л", "price": 29999, "quantity": 5}]}, "порошок")
        answer = await model.select("порошок", found, "порошок")
        sent = model.ask.call_args.args[1]["products"]
        self.assertEqual(len(sent), 1)
        self.assertIn("Гель", sent[0]["name"])
        self.assertNotIn("Порошок 3кг", answer)
        self.assertIn("Гель", answer)

    async def test_all_out_of_stock_skips_model(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock()
        found = normalize({"items": [
            {"name": "Порошок 3кг", "price": 44999, "quantity": 0}]}, "порошок")
        answer = await model.select("порошок", found, "порошок")
        model.ask.assert_not_called()
        self.assertIn("нет в наличии", answer)
        self.assertNotIn("Порошок 3кг", answer)

    async def test_budget_filters_above_price(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([show(), show()]), "stop"))
        found = normalize({"items": [
            {"name": "Порошок дешёвый 1кг", "price": 19999, "quantity": 5},
            {"name": "Порошок дорогой 6кг", "price": 99900, "quantity": 5}]}, "порошок")
        answer = await model.select("порошок", found, "порошок до 200 рублей", budget=200)
        sent = model.ask.call_args.args[1]["products"]
        self.assertEqual(len(sent), 1)
        self.assertIn("дешёвый", sent[0]["name"])
        self.assertNotIn("999,00", answer)
        self.assertIn("199,99", answer)

    async def test_budget_nothing_under_replies_without_model(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock()
        found = normalize({"items": [{"name": "Порошок 6кг", "price": 99900, "quantity": 5}]}, "порошок")
        answer = await model.select("порошок", found, "порошок до 100 рублей", budget=100)
        model.ask.assert_not_called()
        self.assertIn("бюджета не нашлось", answer)

    async def test_budget_unpriced_items_dropped(self):
        model = Model("fake", "fake")
        model.ask = AsyncMock(return_value=(aligned([show()]), "stop"))
        found = normalize({"items": [
            {"name": "Порошок без цены", "quantity": 5},
            {"name": "Порошок 150₽", "price": 15000, "quantity": 5}]}, "порошок")
        await model.select("порошок", found, "порошок до 200 рублей", budget=200)
        sent = model.ask.call_args.args[1]["products"]
        self.assertEqual(len(sent), 1)
        self.assertIn("150", sent[0]["price"])
