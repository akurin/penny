from copy import deepcopy
import unittest

from penny.products import ordered_sections
from penny.rendering import render_sections
from penny.store import normalize


def catalogue():
    return normalize({'items': [
        {'name': 'Молоко А 1 л', 'price': 20000, 'quantity': 3},
        {'name': 'Молоко Б 2 л', 'price': 30000, 'quantity': 3},
        {'name': 'Молоко В 1 л', 'price': 10000, 'quantity': 3},
    ]}, 'молоко')


class RenderingTests(unittest.TestCase):
    def test_rendering_preserves_inputs_and_is_repeatable(self):
        for sort in ('price_asc', 'price_desc', 'popularity'):
            with self.subTest(sort=sort):
                result = {**catalogue(), 'sort': sort}
                sections = [{'title': 'Молоко', 'items': [0, 1, 2], 'note': 'Заметка'}]
                before = deepcopy((result, sections))
                answer = render_sections(result, sections)
                self.assertEqual((result, sections), before)
                self.assertEqual(render_sections(result, sections), answer)

    def test_price_sort_keeps_unknown_prices_last_and_ties_stable(self):
        products = catalogue()['products']
        products[1]['price'] = None
        products[2]['price'] = products[0]['price']
        sections = [{'title': 'Молоко', 'items': [2, 1, 0]}]
        for sort in ('price_asc', 'price_desc'):
            with self.subTest(sort=sort):
                ordered = ordered_sections(products, sections, sort)
                self.assertEqual(ordered[0]['items'], [2, 0, 1])
        self.assertEqual(sections[0]['items'], [2, 1, 0])

    def test_unit_sort_requires_comparable_products(self):
        sections = [{'title': 'Молоко', 'items': [0, 1, 2]}]
        for change in ({'card': True}, {'quantity': None}, {'quantity': 0},
                       {'name': 'Концентрат 1 л'}, {'name': 'Молоко 1 кг'},
                       {'name': 'Молоко 2 x 1 л'}, {'price': None}):
            with self.subTest(change=change):
                products = catalogue()['products']
                products[0].update(change)
                self.assertEqual(ordered_sections(products, sections), sections)

    def test_multiple_sections_keep_model_order_without_explicit_price_sort(self):
        products = catalogue()['products']
        sections = [{'title': 'Первое', 'items': [0, 2]},
                    {'title': 'Второе', 'items': [1]}]
        self.assertEqual(ordered_sections(products, sections), sections)
        ordered = ordered_sections(products, sections, 'price_asc')
        self.assertEqual([s['items'] for s in ordered], [[2, 0], [1]])
