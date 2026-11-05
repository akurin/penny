"""End-to-end live regression: real stores + real DeepSeek, no Telegram.

Run: STORE_PROXY=... python -m tests.live_e2e
Skips (with OK-skip) when STORE_PROXY or DEEPSEEK_API_KEY is missing, so CI
and dry machines do not spend money or hit the network.
"""
import asyncio
import os
import re
import sys
from decimal import Decimal

from dotenv import load_dotenv

from penny.research import research
from penny.service import Model
from penny.store import reply_chunks

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))


async def pipeline(model, proxy, text):
    query, sort, budget = await model.query(text)
    if not query:
        return None, None, None
    result = await research(model, query, text, proxy, sort=sort)
    answer = await model.select(query, result, original_request=text, budget=budget)
    return query, result, answer


def strip_tags(html):
    return re.sub(r"<[^>]+>", "", html)


def store_lines(answer):
    """(name, price, store) triples parsed from rendered cards, tagged-safe."""
    cards = []
    for block in answer.split("\n\n"):
        block = strip_tags(block)
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(lines) < 2 or not re.match(r"\d+\. ", lines[0]):
            continue
        name = lines[0].split(". ", 1)[1].strip()
        price = store = None
        for ln in lines[1:]:
            m = re.match(r"(\d[\d.,]*) ₽", ln)
            if m and price is None:
                price = Decimal(m.group(1).replace(".", "").replace(",", "."))
            if ln.startswith("📍 "):
                store = ln[2:].strip()
        cards.append((name, price, store))
    return cards


def per_section_prices_ascending(answer):
    """Within each section, priced cards must be in ascending order (cheapest intent)."""
    return _per_section_prices(answer, reverse=False)


def per_section_prices_descending(answer):
    """Within each section, priced cards must be in descending order (priciest intent)."""
    return _per_section_prices(answer, reverse=True)


def _per_section_prices(answer, reverse):
    sections, current = [], None
    for block in answer.split("\n\n"):
        text = strip_tags(block).strip()
        lines = [ln for ln in text.split("\n") if ln.strip()]
        if not lines:
            continue
        if not re.match(r"\d+\. ", lines[0]) and not lines[0].startswith("📍") and not lines[0].startswith("—"):
            current = []
            sections.append(current)
            continue
        if re.match(r"\d+\. ", lines[0]) and current is not None:
            for ln in lines[1:]:
                m = re.match(r"(\d[\d.,]*) ₽", ln)
                if m:
                    current.append(Decimal(m.group(1).replace(".", "").replace(",", ".")))
                    break
    ok = all(prices == sorted(prices, reverse=reverse) for prices in sections if len(prices) > 1)
    return ok, f"sections={[[str(p) for p in s] for s in sections]}"


def no_prices_outside_cards(answer):
    """Every ₽ must be inside a rendered card; conclusion/notes/footer are prose."""
    for block in answer.split("\n\n"):
        text = strip_tags(block).strip()
        if not text:
            continue
        if re.match(r"\d+\. ", text):
            continue
        if text.startswith(("Выгоднее ", "Дешевле всего ", "Дороже всего ", "Дороже за ",
                            "Одинаков")):
            continue  # code-rendered best-value line, prices validated by code
        if re.search(r"\d+(?:,\d\d)? ₽", text):
            return False
    return True


CASES = [
    {
        "name": "brand_only_does_not_gain_category",
        "text": "Харрис",
        "expect": None,
        "expect_query": lambda q: q is not None and q.casefold() == "харрис",
    },
    {
        "name": "brand_with_form_word",
        "text": "салфетки первым делом",
        "expect": lambda q, r, a: (
            # Brand acknowledged: shown as cards or honestly explained in prose.
            any("Первым делом" in n for n, _, _ in store_lines(a))
            or "Первым делом" in strip_tags(a),
            "запрошенный бренд не должен быть проигнорирован молча"),
        "expect_query": lambda q: (q is not None and "алфет" in q.lower()
                                   and "первым делом" in q.casefold()),
    },
    {
        "name": "unsupported_is_rejected",
        "text": "привет, как дела?",
        "expect": lambda q, r, a: (q is None, "не-shopping запрос должен дать query=None"),
    },
    {
        "name": "other_store_rejected",
        "text": "сколько стоит молоко в Перекрёстке",
        "expect": lambda q, r, a: (q is None, "чужой магазин → query=None"),
    },
    {
        "name": "cross_store_comparison",
        "text": "ушастый нянь",
        "expect": lambda q, r, a: (
            len({s for _, _, s in store_lines(a)}) >= 1 and len(store_lines(a)) >= 3,
            "ожидается несколько карточек бренда"),
        "expect_footer": lambda a: (
            True, "футер перечисляет только магазины с товарами"),
    },
    {
        "name": "hypoallergenic_split",
        "text": "стиральный порошок гипоаллергенный",
        "expect": lambda q, r, a: (
            "гипоаллергенн" in strip_tags(a).lower(),
            "свойство должно быть отражено в заголовках/выводе"),
    },
    {
        "name": "cheapest_intent",
        "text": "самый дешевый гель для душа",
        "expect": lambda q, r, a: per_section_prices_ascending(a),
    },
    {
        "name": "out_of_stock_hidden",
        "text": "зубная нить Splat",
        "expect": lambda q, r, a: (
            "нет в наличии" not in a,
            "показываем только наличие; out-of-stock скрыт до рендера"),
    },
    {
        "name": "no_price_hallucination",
        "text": "протеиновый батончик",
        "expect": lambda q, r, a: (no_prices_outside_cards(a),
                                   "₽ вне карточек = модель пишет цены"),
    },
    {
        "name": "budget_intent",
        "text": "порошок до 200 рублей",
        "expect": lambda q, r, a: (
            (lambda prices: not prices or max(prices) <= 200)([p for _, p, _ in store_lines(a) if p is not None]),
            "все показанные цены должны быть в бюджете"),
    },
    {
        "name": "priciest_intent",
        "text": "самый дорогой шампунь",
        "expect": lambda q, r, a: per_section_prices_descending(a),
    },
    {
        "name": "all_stores_empty",
        "text": "аспирин",
        "expect": lambda q, r, a: (
            # Honest outcome either way: all-empty message or "nothing matched" verdict
            # with unrelated fuzzy matches hidden (aspirin is not sold in these stores).
            "не вернул товаров" in strip_tags(a)
            or ("Подходящих товаров в найденном нет" in strip_tags(a)
                and not store_lines(a)),
            "аптечный товар не должен рендериться как совпадение"),
    },
    {
        "name": "brand_punctuation",
        "text": "зубная щетка R.O.C.S.",
        "expect": None,
        "expect_query": lambda q: q is not None and "R.O.C.S" in q,
    },
    {
        "name": "emoji_in_request",
        "text": "крем для рук 🧴",
        "expect": None,
    },
]

GROUND_TRUTH = [
    # (name, product-name-substring, store, expected price)
    ("tide_toddler", "Tide детский гипоаллергенный", "М.Косметик", Decimal("399.99")),
    ("ushastyi_powder", "Стиральный порошок Ушастый нянь детский 2.4кг", "Магнит", None),  # price drifts
]


async def main():
    load_dotenv(".env")
    proxy = os.environ.get("STORE_PROXY")
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not proxy or not key:
        print("SKIP: STORE_PROXY / DEEPSEEK_API_KEY missing")
        return 0
    model = Model(key, os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    results = await asyncio.gather(
        *(pipeline(model, proxy, case["text"]) for case in CASES),
        return_exceptions=True)
    for case, outcome in zip(CASES, results):
        if isinstance(outcome, BaseException):
            check(case["name"] + ":pipeline", False, repr(outcome))
            continue
        query, result, answer = outcome
        if isinstance(query, BaseException):
            check(case["name"] + ":pipeline", False, repr(query))
            continue
        if case.get("expect_query"):
            outcome = case["expect_query"](query)
            ok, detail = outcome if isinstance(outcome, tuple) else (outcome, "")
            check(f"{case['name']}:query", ok, f"query={query!r} {detail}")
        if case["expect"] is None:
            check(f"{case['name']}:pipeline", query is not None, f"query={query!r}")
            continue
        outcome = case["expect"](query, result, answer)
        ok, detail = outcome if isinstance(outcome, tuple) else (outcome, "")
        check(f"{case['name']}:expect", ok, detail)
        if not ok:
            print("   query:", query)
            print("   answer:", strip_tags(answer)[:600].replace("\n", " | "))
        # universal invariants
        if answer:
            for chunk in reply_chunks(answer):
                if len(chunk) > 3500:
                    check(f"{case['name']}:chunk_limit", False, "chunk too long")
            cards = store_lines(answer)
            if cards:
                names_with_price = [c for c in cards if c[1] is not None]
                check(f"{case['name']}:prices_rendered",
                      len(names_with_price) >= len(cards) - 1,
                      f"cards without price: {len(cards) - len(names_with_price)}")
                stores_shown = {s for _, _, s in cards}
                # footer stores must be subset of card stores unless no cards at all
                footer_stores = set(re.findall(r"📍 (М\.Косметик|Магнит|Дикси) · ", strip_tags(answer)))
                if cards:
                    check(f"{case['name']}:footer_subset",
                          footer_stores <= stores_shown,
                          f"footer={footer_stores} cards={stores_shown}")
            conclusion = strip_tags(answer)
            check(f"{case['name']}:no_tooffee_style_filler",
                  "Сравнивать не с чем" not in conclusion, "filler phrase leaked")
    failed = [c for c, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
