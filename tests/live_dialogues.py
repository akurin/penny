"""Real-model conversational regression without Telegram or store requests.

Run: ./scripts/uv run --locked python -m tests.live_dialogues
Uses DeepSeek, but synthetic products and local session state keep the run cheap and
deterministic with respect to stores.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

from penny.bot import conversation_answer, session_answer
from penny.service import Model


HARRIS_PRODUCTS = [
    {"name": "Хлеб пшеничный нарезанный Harrys 470 г", "price": "129.99", "store": "Магнит"},
    {"name": "Хлеб тостовый Harry's пшеничный 470 г", "price": "143.90", "store": "Дикси"},
    {"name": "Хлеб Harry's American Sandwich 470 г", "price": "139.99", "store": "Пятёрочка"},
    {"name": "Хлеб HARRY'S American sandwich 470 г", "price": "119.99", "store": "Лента"},
    {"name": "Хлеб HARRY'S с отрубями 515 г", "price": "119.99", "store": "Лента"},
]

POPCORN_PRODUCTS = [
    {"name": "Попкорн Corincorn сладко-солёный 85 г", "price": "69.99", "store": "Магнит"},
    {"name": "Попкорн OUCH сырный 85 г", "price": "99.90", "store": "Дикси"},
    {"name": "Попкорн Mixbar сырный 85 г", "price": "51.99", "store": "Пятёрочка"},
    {"name": "Попкорн HOLY CORN морская соль 65 г", "price": "99.99", "store": "Лента"},
    {"name": "Попкорн Скрепыши сырный 85 г", "price": "54.99", "store": "Магнит"},
]


def product(name, price, store="Магнит"):
    return {
        "site": "magnit", "store": store, "id": name, "name": name,
        "price": price, "old_price": None, "quantity": 1, "card": False,
        "checked": "31.10.2026 18:00 МСК", "source": "https://magnit.ru/",
    }


DIALOGUES = [
    ("harris_followups", [
        {"message": "харрис", "query": "харрис", "continued": False},
        {"message": "где дешевле?", "query": "харрис", "continued": True,
         "sort": "price_asc"},
        {"message": "...акая средняя цена?", "query": "харрис", "continued": True,
         "route": "results", "result_metric": "price", "result_aggregation": "average",
         "answer_contains": "130,77 ₽"},
        {"message": "сколько вариантов показал?", "query": "харрис", "continued": True,
         "route": "results", "result_metric": "product", "result_aggregation": "count",
         "answer_contains": "5"},
        {"message": "в каких магазинах были показанные варианты?", "query": "харрис",
         "continued": True, "route": "results", "result_metric": "store",
         "result_aggregation": "list", "answer_contains": "Магнит"},
        {"message": "какая минимальная цена среди показанных?", "query": "харрис",
         "continued": True, "route": "results", "result_metric": "price",
         "result_aggregation": "minimum", "answer_contains": "119,99 ₽"},
        {"message": "какой состав?", "query": "харрис", "continued": True,
         "route": "external_info", "info_topic": "composition", "answer_contains": "состав",
         "answer_not_contains": "Huggies"},
        {"message": "привет", "query_none": True, "route": "conversation",
         "conversation_kind": "scope"},
        {"message": "молоко", "query": "молоко", "continued": False,
         "sort": "popularity", "budget": None},
    ]),
    ("constraints_and_refresh", [
        {"message": "а где дешевле?", "query_none": True, "route": "conversation",
         "conversation_kind": "scope"},
        {"message": "зубная нить Splat", "query_contains": "нить", "continued": False},
        {"message": "только Splat", "query": "зубная нить Splat", "continued": True,
         "request_contains": "только"},
        {"message": "до 200 рублей", "query": "зубная нить Splat", "continued": True,
         "sort": "price_asc", "budget": 200, "request_contains": "только"},
        {"message": "обнови цены", "query": "зубная нить Splat", "continued": True,
         "refresh": True, "request_contains": "только"},
        {"message": "кофе", "query": "кофе", "continued": False,
         "sort": "popularity", "budget": None},
        {"message": "Харрис", "query": "харрис", "continued": False,
         "query_not_contains": "кофе", "sort": "popularity", "budget": None},
    ]),
    ("comparison_and_store", [
        {"message": "стиральный порошок", "query_contains": "порош", "continued": False},
        {"message": "а в Пятёрочке?", "query_contains": "порош", "continued": True,
         "request_contains": "пят"},
        {"message": "какой выгоднее?", "query_contains": "порош", "continued": True,
         "request_contains": "пят", "route": "results", "result_metric": "unit_price",
         "result_aggregation": "minimum"},
        {"message": "сравни цены", "query": "стиральный порошок", "continued": True,
         "sort": "price_asc", "request_contains": "пят", "route": "catalog"},
        {"message": "R.O.C.S.", "query_contains": "r.o.c.s", "continued": False},
    ]),
    ("unsupported_and_short_names", [
        {"message": "привет, как дела?", "query_none": True, "route": "conversation",
         "conversation_kind": "scope"},
        {"message": "сколько стоит молоко в Перекрёстке", "query_none": True,
         "route": "conversation", "conversation_kind": "scope"},
        {"message": "флосс", "query_present": True, "continued": False},
        {"message": "тофу", "query": "тофу", "continued": False},
        {"message": "а без сахара?", "query_contains": "тофу", "continued": True,
         "request_contains": "без сахара"},
    ]),
    ("standalone_information", [
        {"message": "хлеб харрис, состав", "query_contains": "харрис",
         "continued": False, "route": "external_info", "info_topic": "composition",
         "answer_contains": "состав",
         "answer_not_contains": "Напишите название товара ещё раз"},
        {"message": "место производства известно?", "query_contains": "харрис",
         "continued": True, "route": "external_info", "info_topic": "origin",
         "answer_contains": "место производства", "answer_not_contains": "состав"},
        {"message": "а что знаешь?", "query_contains": "харрис",
         "continued": True, "route": "external_info", "info_topic": "general",
         "answer_contains": "название, цену"},
        {"message": "у тебя нет источника?", "query_none": True,
         "route": "conversation", "conversation_kind": "capabilities",
         "answer_contains": "Внешний источник"},
        {"message": "ок", "query_none": True, "route": "conversation",
         "conversation_kind": "acknowledgement", "answer_contains": "Рада помочь"},
    ]),
    ("context_survives_smalltalk", [
        {"message": "сыр", "query_contains": "сыр", "continued": False},
        {"message": "ok", "query_none": True, "route": "conversation",
         "conversation_kind": "acknowledgement", "answer_contains": "Рада помочь"},
        {"message": "а сколько вариантов ты показала?", "query_contains": "сыр",
         "continued": True, "route": "results", "result_metric": "product",
         "result_aggregation": "count", "answer_contains": "1"},
        {"message": "а откуда ты берёшь цены?", "query_none": True,
         "route": "conversation", "conversation_kind": "capabilities",
         "answer_contains": "каталоги"},
        {"message": "покажи сначала самые дорогие", "query_contains": "сыр",
         "continued": True, "route": "catalog", "sort": "price_desc"},
        {"message": "нет, теперь масло", "query_contains": "масл",
         "continued": False, "sort": "popularity", "budget": None},
    ]),
    ("unsupported_store_preserves_context", [
        {"message": "молоко", "query": "молоко", "continued": False},
        {"message": "а в Ашане?", "query_none": True, "route": "conversation",
         "conversation_kind": "scope"},
        {"message": "тогда где дешевле?", "query": "молоко", "continued": True,
         "route": "catalog", "sort": "price_asc"},
        {"message": "какая самая высокая цена из показанных?", "query": "молоко",
         "continued": True, "route": "results", "result_metric": "price",
         "result_aggregation": "maximum", "answer_contains": "100,00 ₽"},
        {"message": "сколько магазинов в результате?", "query": "молоко",
         "continued": True, "route": "results", "result_metric": "store",
         "result_aggregation": "count", "answer_contains": "1"},
    ]),
    ("constraints_change_and_reset", [
        {"message": "йогурт без сахара", "query_contains": "йогурт",
         "continued": False},
        {"message": "нет, нужен питьевой", "query_contains": "питьев",
         "continued": True, "request_contains": "питьевой"},
        {"message": "не дороже 150 рублей", "query_contains": "йогурт",
         "continued": True, "sort": "price_asc", "budget": 150},
        {"message": "убери ограничение по цене", "query_contains": "йогурт",
         "continued": True, "budget": None},
        {"message": "теперь творог", "query_contains": "творог",
         "continued": False, "sort": "popularity", "budget": None},
    ]),
    ("information_then_back_to_search", [
        {"message": "кофе, где произведён?", "query_contains": "кофе",
         "continued": False, "route": "external_info", "info_topic": "origin",
         "answer_contains": "место производства"},
        {"message": "а состав известен?", "query_contains": "кофе",
         "continued": True, "route": "external_info", "info_topic": "composition",
         "answer_contains": "состав"},
        {"message": "ладно", "query_none": True, "route": "conversation",
         "conversation_kind": "acknowledgement", "answer_contains": "Рада помочь"},
        {"message": "покажи кофе", "query_contains": "кофе", "continued": False,
         "route": "catalog"},
        {"message": "в каких магазинах он есть?", "query_contains": "кофе",
         "continued": True, "route": "results", "result_metric": "store",
         "result_aggregation": "list", "answer_contains": "Магнит"},
    ]),
    ("no_context_and_negation", [
        {"message": "что из них дешевле?", "query_none": True,
         "route": "conversation", "conversation_kind": "scope"},
        {"message": "okay, спасибо", "query_none": True, "route": "conversation",
         "conversation_kind": "acknowledgement", "answer_contains": "Рада помочь"},
        {"message": "подгузники", "query_contains": "подгуз", "continued": False},
        {"message": "только не Huggies", "query_contains": "подгуз",
         "continued": True, "request_contains": "не Huggies"},
        {"message": "а сколько стоит самый дешёвый из показанных?",
         "query_contains": "подгуз", "continued": True, "route": "results",
         "result_metric": "price", "result_aggregation": "minimum",
         "answer_contains": "100,00 ₽"},
    ]),
    ("lexically_specific_queries", [
        {"message": "салфетки первым делом", "query_contains": "первым делом",
         "continued": False},
        {"message": "щётка R.O.C.S. для чувствительных зубов",
         "query_contains": "r.o.c.s", "continued": False},
        {"message": "гель для душа без запаха", "query_contains": "без запаха",
         "continued": False},
    ]),
    ("recommendation_and_unit_price", [
        {"message": "попокорн", "query_contains": "поп", "continued": False},
        {"message": "какой дешевле?", "query_contains": "поп", "continued": True,
         "route": "catalog", "sort": "price_asc"},
        {"message": "что еще похожее попробовать?", "query_contains": "поп",
         "continued": True, "route": "catalog"},
        {"message": "а что лучше для ребенка?", "query_contains": "поп",
         "continued": True, "route": "external_info", "info_topic": "suitability",
         "answer_contains": "нельзя надёжно оценить"},
        {"message": "выбери один", "query_contains": "поп", "continued": True,
         "route": "external_info", "info_topic": "suitability",
         "answer_contains": "возрастной маркировки"},
        {"message": "тогда самый выгодный", "query_contains": "поп",
         "continued": True, "route": "results", "result_metric": "unit_price",
         "result_aggregation": "minimum", "answer_contains": "611,65 ₽/кг"},
        {"message": "а за кг?", "query_contains": "поп", "continued": True,
         "route": "results", "result_metric": "unit_price",
         "result_aggregation": "minimum", "answer_contains": "611,65 ₽/кг"},
    ]),
]


def normalized(value):
    return value.casefold().strip() if isinstance(value, str) else value


def synthetic_products(query, previous):
    if "харрис" in normalized(query) or "harris" in normalized(query):
        return HARRIS_PRODUCTS
    if "попкорн" in normalized(query) or "попокорн" in normalized(query):
        return POPCORN_PRODUCTS
    return [{"name": query, "price": "100.00", "store": "Магнит"}]


def turn_state(intent):
    return {
        "route": intent.route,
        "result_metric": intent.result_metric,
        "result_aggregation": intent.result_aggregation,
        "info_topic": intent.info_topic,
    }


def check_expectation(intent, expected, previous):
    errors = []
    query = normalized(intent.query)
    if expected.get("query_none") and intent.query is not None:
        errors.append(f"expected query=None, got {intent.query!r}")
    if expected.get("query_present") and intent.query is None:
        errors.append("expected a query")
    if "query" in expected and query != normalized(expected["query"]):
        errors.append(f"query={intent.query!r}, expected {expected['query']!r}")
    if "query_contains" in expected and normalized(expected["query_contains"]) not in (query or ""):
        errors.append(f"query={intent.query!r} lacks {expected['query_contains']!r}")
    if "query_not_contains" in expected and normalized(expected["query_not_contains"]) in (query or ""):
        errors.append(f"query={intent.query!r} contains forbidden {expected['query_not_contains']!r}")
    for field in ("continued", "sort", "budget", "refresh", "route",
                  "result_metric", "result_aggregation", "info_topic", "conversation_kind"):
        if field in expected and getattr(intent, field) != expected[field]:
            errors.append(f"{field}={getattr(intent, field)!r}, expected {expected[field]!r}")
    if "request_contains" in expected:
        wanted = normalized(expected["request_contains"])
        if wanted not in normalized(intent.request):
            errors.append(f"request={intent.request!r} lacks {expected['request_contains']!r}")
    if intent.route == "conversation":
        answer = conversation_answer(intent.conversation_kind)
        if expected.get("answer_contains") and expected["answer_contains"] not in (answer or ""):
            errors.append(f"answer lacks {expected['answer_contains']!r}: {answer!r}")
    elif intent.route != "catalog":
        answer = session_answer(
            intent, previous.get("products") if intent.continued and previous else None)
        if expected.get("answer_contains") not in (None, "") and expected["answer_contains"] not in answer:
            errors.append(f"answer lacks {expected['answer_contains']!r}: {answer!r}")
        if expected.get("answer_not_contains") and expected["answer_not_contains"] in answer:
            errors.append(f"answer contains {expected['answer_not_contains']!r}: {answer!r}")
    return errors


async def run_dialogue(model, name, turns):
    previous = None
    outcomes = []
    for turn in turns:
        message = turn["message"]
        intent = await model.interpret(message, previous)
        errors = check_expectation(intent, turn, previous)
        outcomes.append((name, message, intent, errors))
        if intent.query is not None and intent.route == "catalog":
            previous = {
                "request": intent.request,
                "query": intent.query,
                "sort": intent.sort,
                "budget": intent.budget,
                "products": synthetic_products(intent.query, previous),
                "turn": turn_state(intent),
            }
        elif intent.query is not None and not intent.continued:
            previous = {
                "request": intent.request, "query": intent.query, "sort": intent.sort,
                "budget": intent.budget, "products": [], "turn": turn_state(intent),
            }
        elif intent.query is not None and previous is not None:
            previous = {**previous, "turn": turn_state(intent)}
    return outcomes


async def run_selection_check(model):
    products = [
        product("Хлеб пшеничный Harry's American Sandwich 470 г", "119.99"),
        product("Хлеб Harrys тостовый 470 г", "129.99"),
        product("Трусики-подгузники Huggies Dry Expert 17 шт", "399.99"),
        product("Пиво Harbin Premium 0,61 л", "229.99"),
    ]
    result = {
        "products": products, "sites": ["magnit"],
        "site_urls": {"magnit": "https://magnit.ru/"}, "notes": [],
        "incomplete": False, "sort": "popularity",
    }
    answer, remembered = await model.select(
        "Харрис", result, original_request="Харрис", with_meta=True)
    errors = []
    names = "\n".join(item["name"] for item in remembered)
    if "Harry" not in names:
        errors.append(f"Harry's missing from remembered products: {names!r}")
    for forbidden in ("Huggies", "Harbin"):
        if forbidden in answer or forbidden in names:
            errors.append(f"{forbidden} leaked into selected answer")
    return answer, errors


async def main():
    load_dotenv(".env")
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        print("SKIP: DEEPSEEK_API_KEY missing")
        return 0
    model = Model(key, os.environ.get("DEEPSEEK_MODEL", "deepseek-flash"))
    rounds = max(1, int(os.environ.get("PENNY_DIALOGUE_ROUNDS", "1")))
    verbose = os.environ.get("PENNY_DIALOGUE_VERBOSE") == "1"
    failed = 0
    total = 0
    for round_number in range(1, rounds + 1):
        groups = await asyncio.gather(
            *(run_dialogue(model, name, turns) for name, turns in DIALOGUES))
        for outcomes in groups:
            for name, message, intent, errors in outcomes:
                if verbose or errors:
                    status = "PASS" if not errors else "FAIL"
                    print(f"{status} r{round_number} {name}: {message!r} -> {intent}")
                    for error in errors:
                        print("     ", error)
                failed += bool(errors)
                total += 1
        answer, selection_errors = await run_selection_check(model)
        if verbose or selection_errors:
            status = "PASS" if not selection_errors else "FAIL"
            print(f"{status} r{round_number} selection: {answer!r}")
            for error in selection_errors:
                print("     ", error)
        failed += bool(selection_errors)
        total += 1
        print(f"round {round_number}: {total - failed}/{total} checks passed")
    print(f"\n{total - failed}/{total} dialogue and selection checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
