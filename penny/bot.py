import math
import uuid
import asyncio
import os
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (Message, LinkPreviewOptions, BufferedInputFile,
                           BotCommand, InlineKeyboardButton, InlineKeyboardMarkup)
from aiogram.exceptions import TelegramNetworkError
from .store import DIXY_STATE_PATH
from .products import unit_price
from .rendering import reply_chunks, money
from .service import Model, model_settings
from .research import research
from .telegram_connection import wait_for_telegram, PollingHealth
from .logging_setup import configure, request_id, event, content, failure, span
from .memory import Memory, NullMemory
from .watches import send_drop_alert


def allowed(user_id, chat_type, ids):
    return chat_type == "private" and user_id in ids


def telegram_session():
    """Send Bot API calls through TELEGRAM_PROXY when it is set.

    aiogram's aiohttp session ignores HTTP(S)_PROXY, so the proxy has to be passed
    explicitly. aiohttp-socks resolves api.telegram.org at the proxy, which keeps a
    poisoned local answer from breaking the connection.
    """
    proxy = os.getenv("TELEGRAM_PROXY")
    if not proxy:
        return None
    # aiohttp-socks only knows socks5:// and resolves at the proxy anyway, so a
    # socks5h:// value written for the store side works here unchanged.
    if proxy.startswith("socks5h://"):
        proxy = "socks5://" + proxy[len("socks5h://"):]
    return AiohttpSession(proxy=proxy)


# Registered with Telegram so the commands are discoverable in the "/" menu.
COMMANDS = (
    BotCommand(command="start", description="Начать и краткая справка"),
    BotCommand(command="help", description="Что умеет Penny и как её просить"),
    BotCommand(command="watch", description="Следить за ценой товара"),
    BotCommand(command="unwatch", description="Убрать подписку"),
    BotCommand(command="history", description="История цены товара"),
    BotCommand(command="dixy_captcha", description="Разблокировать Дикси по капче"),
)


def cooldown_remaining(last_started, now, seconds=10):
    if last_started is None:
        return 0
    return max(0, math.ceil(seconds - (now - last_started)))


def captcha_keyboard(result):
    """Offer the captcha relay as a button when Дикси needs a human."""
    if "dixy" not in (result.get("captcha_sites") or []):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Пройти капчу Дикси", callback_data="dixy_captcha")]])


def product_keyboard(products, prefix, skip=()):
    """Buttons for the last reply's products, so a number need not be typed.

    skip holds (site, product_key) pairs already watched; those are not offered
    again because re-watching would only move their target price."""
    skip = set(skip)
    rows = []
    for index, product in enumerate(products, 1):
        if product.get("price") is None:
            continue
        if (product.get("site"), Memory.product_key(product)) in skip:
            continue
        label = f"{index}. {product.get('name', '')}"
        label = label if len(label) <= 52 else label[:51] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{index}")])
        if len(rows) == 9:
            break
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def watch_list_text(watches):
    rows = ["<b>Слежу за ценами:</b>"]
    for index, watch in enumerate(watches, 1):
        store = f" · {escape(watch['store'])}" if watch.get("store") else ""
        rows.append(f"{index}. {escape(watch['name'])}{store} — сообщу ниже "
                    f"{money(watch['target_price'])} ₽")
    return "\n".join(rows)


def unwatch_keyboard(watches):
    rows = [[InlineKeyboardButton(text=f"Убрать {index}",
                                  callback_data=f"unwatch:{watch['id']}")]
            for index, watch in enumerate(watches, 1)]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def session_answer(intent, products):
    """Answer from saved results or explain when catalogue data is insufficient."""
    products = [p for p in (products or [])
                if isinstance(p, dict) and isinstance(p.get("name"), str)]
    if intent.route == "results":
        if not products:
            return ("Не сохранила подходящие товары из предыдущего ответа. "
                    "Напишите название товара ещё раз.")
        if intent.result_metric == "product" and intent.result_aggregation == "count":
            return f"В предыдущем ответе показано вариантов: <b>{len(products)}</b>."
        if intent.result_metric == "product" and intent.result_aggregation == "choose":
            return ("По какому критерию выбрать один вариант: самая низкая цена за упаковку, "
                    "цена за кг/л или конкретный состав и свойства?")
        stores = list(dict.fromkeys(str(p["store"]) for p in products if p.get("store")))
        if intent.result_metric == "store" and intent.result_aggregation == "list":
            return "Показанные варианты нашлись в магазинах: " + ", ".join(map(escape, stores)) + "."
        if intent.result_metric == "store" and intent.result_aggregation == "count":
            return f"Показанные варианты нашлись в магазинах: <b>{len(stores)}</b>."
        if intent.result_metric == "unit_price":
            by_unit = {}
            for product in products:
                try:
                    value = unit_price(product)
                except (InvalidOperation, KeyError, TypeError, ValueError):
                    value = None
                if value:
                    by_unit.setdefault(value[1], []).append((value[0], product))
            if not by_unit:
                return ("В сохранённых карточках недостаточно однозначных данных об упаковке, "
                        "чтобы посчитать цену за кг или литр.")
            rows = []
            for unit, values in by_unit.items():
                if intent.result_aggregation == "list":
                    if len(by_unit) > 1:
                        rows.append(f"Цена за {unit}:")
                    for value, product in sorted(values, key=lambda row: row[0]):
                        rows.append(f"• {money(value)} ₽/{unit} — {escape(product['name'])}")
                    continue
                if intent.result_aggregation == "average":
                    value = sum((row[0] for row in values), Decimal(0)) / len(values)
                    rows.append(f"Средняя цена — <b>{money(value)} ₽/{unit}</b>.")
                    continue
                reverse = intent.result_aggregation == "maximum"
                value, product = (max(values, key=lambda row: row[0]) if reverse
                                  else min(values, key=lambda row: row[0]))
                label = "Самая высокая" if reverse else "Самая выгодная"
                rows.append(
                    f"{label} цена за {unit} — <b>{money(value)} ₽/{unit}</b>: "
                    f"{escape(product['name'])}.")
            return "\n".join(rows)
        priced = []
        for product in products:
            try:
                if product.get("price") is not None:
                    priced.append((Decimal(str(product["price"])), product))
            except InvalidOperation:
                pass
        if intent.result_metric != "price" or not priced:
            return "По сохранённым карточкам этот показатель посчитать нельзя."
        if intent.result_aggregation == "list":
            rows = ["Цены показанных вариантов:"]
            for price, product in sorted(priced, key=lambda row: row[0])[:9]:
                store = f" — {escape(str(product['store']))}" if product.get("store") else ""
                rows.append(f"• {money(price)} ₽ — {escape(product['name'])}{store}")
            return "\n".join(rows)
        if intent.result_aggregation == "average":
            value = sum((price for price, _ in priced), Decimal(0)) / len(priced)
            return (f"Средняя цена среди показанных вариантов ({len(priced)}) — "
                    f"<b>{money(value)} ₽</b> за упаковку.")
        if intent.result_aggregation in ("minimum", "maximum"):
            price, product = (min(priced, key=lambda row: row[0])
                              if intent.result_aggregation == "minimum"
                              else max(priced, key=lambda row: row[0]))
            label = "Минимальная" if intent.result_aggregation == "minimum" else "Максимальная"
            return f"{label} цена — <b>{money(price)} ₽</b>: {escape(product['name'])}."
        return "По сохранённым карточкам этот показатель посчитать нельзя."
    if intent.route == "external_info":
        subject = f" «{escape(intent.query)}»" if intent.query else ""
        if intent.info_topic == "composition":
            return (f"В данных магазинов состав{subject} не указан. Точный состав нужно "
                    "проверять по карточке производителя; придумывать его не буду.")
        if intent.info_topic == "origin":
            return (f"В данных магазинов место производства{subject} не указано. "
                    "Без источника производителя утверждать его не буду.")
        if intent.info_topic == "suitability":
            return (f"По карточкам магазинов нельзя надёжно оценить пригодность{subject}: "
                    "нет полного состава, аллергенов и возрастной маркировки. Могу сравнить "
                    "показанные варианты по цене за упаковку или за кг/л.")
        return (f"По магазинным данным о{subject} знаю только название, цену, размер "
                "упаковки, наличие и магазин. Для состава, производителя и других "
                "характеристик нужен отдельный источник.")
    raise ValueError("Unknown session question")


def conversation_answer(kind):
    if kind == "acknowledgement":
        return "Рада помочь!"
    if kind == "capabilities":
        return ("Сейчас мои источники — каталоги М.Косметик, Магнита, Дикси, "
                "Пятёрочки и Ленты. В них есть цены, упаковка и иногда наличие, "
                "но нет надёжных данных о составе и производителе. Внешний источник "
                "характеристик пока не подключён.")
    return ("Я помогаю искать и сравнивать товары в пяти магазинах рядом с домом. "
            "Напишите товар или бренд, например «зубная нить Splat».")


def intent_turn(intent):
    """Persist discourse state separately from the catalogue selection request."""
    return {
        "route": intent.route,
        "result_metric": intent.result_metric,
        "result_aggregation": intent.result_aggregation,
        "info_topic": intent.info_topic,
    }


async def answer_with_retry(answer, text, *args, attempts=2, **kwargs):
    """Telegram blips are transient; one retry delivers replies that matter
    (unsupported, progress, results) instead of failing the whole request."""
    for attempt in range(attempts):
        try:
            return await answer(text, *args, **kwargs)
        except TelegramNetworkError:
            if attempt == attempts - 1:
                raise
            event("telegram_send_retry", attempt=attempt)
            await asyncio.sleep(2)


async def run():
    load_dotenv()
    from .store import ensure_ca_bundle
    from .store_config import load_store_config
    # .env is read above, so PENNY_STORES_CONFIG from it applies here.
    configured = load_store_config(os.getenv("PENNY_STORES_CONFIG") or None)
    ensure_ca_bundle()
    configure()
    event("stores_config", loaded=configured)
    required = ("TELEGRAM_BOT_TOKEN", "ALLOWED_USER_IDS", "STORE_PROXY")
    if any(not os.getenv(key) for key in required):
        raise SystemExit("Fill required settings in .env; see .env.example")
    key, model_name, base_url, model_proxy = model_settings()
    if not key:
        raise SystemExit("Fill MODEL_API_KEY in .env; see .env.example")
    ids = {int(value.strip()) for value in os.environ["ALLOWED_USER_IDS"].split(",")}
    if len(ids) > 2 or any(i <= 0 for i in ids):
        raise SystemExit("Configure one or two positive Telegram user IDs")
    model = Model(key, model_name, base_url, model_proxy)
    try:
        memory = Memory()
    except Exception as exc:
        # A broken data directory must not take down price lookups.
        failure("memory_startup", exc)
        memory = NullMemory()
    dispatcher = Dispatcher()
    busy, last = set(), {}
    captcha_waiters = {}  # user id -> Future awaiting the typed captcha text
    captcha_lock = asyncio.Lock()  # one captcha browser at a time
    captcha_active = set()  # users with a relay running (no normal queries)
    last_captcha = {}

    async def handle_dixy_captcha(message, user):
        """Relay Дикси's image captcha: the human reads it, the bot only submits."""
        from .dixy_captcha import ANSWER_TIMEOUT_S, unblock
        if captcha_lock.locked():
            await answer_with_retry(message.answer,
                                    "Разблокировка Дикси уже идёт. Дождитесь её окончания.")
            return
        remaining = cooldown_remaining(last_captcha.get(user), time.monotonic(), 60)
        if remaining:
            await answer_with_retry(message.answer,
                                    f"Разблокировку можно повторить через {remaining} сек.")
            return
        async with captcha_lock:
            last_captcha[user] = time.monotonic()
            captcha_active.add(user)
            token = request_id.set(uuid.uuid4().hex[:12])
            status = "Не удалось открыть Дикси. Проверьте прокси и повторите."
            try:
                await answer_with_retry(
                    message.answer,
                    "Открываю Дикси. Если появится капча — пришлю картинку, введите текст с неё.")

                async def send_image(png):
                    await message.answer_photo(
                        BufferedInputFile(png, filename="dixy_captcha.png"),
                        caption=("Введите текст с картинки как есть (важен регистр). "
                                 f"Ответьте одним сообщением за {ANSWER_TIMEOUT_S // 60} мин."))

                async def get_answer():
                    future = asyncio.get_running_loop().create_future()
                    captcha_waiters[user] = future
                    try:
                        return await asyncio.wait_for(future, ANSWER_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        return None
                    finally:
                        captcha_waiters.pop(user, None)

                async def notify(text):
                    await answer_with_retry(message.answer, text)

                result_code, status = await unblock(os.environ["STORE_PROXY"],
                                                    DIXY_STATE_PATH, send_image,
                                                    get_answer, notify)
                event("dixy_captcha_done", result=result_code)
            except Exception as exc:
                failure("dixy_captcha", exc)
            finally:
                captcha_waiters.pop(user, None)
                captcha_active.discard(user)
                request_id.reset(token)
            await answer_with_retry(message.answer, status)

    def session_products(user):
        try:
            session = memory.get_session(user)
        except Exception as exc:
            failure("memory_session_read", exc)
            return []
        return (session or {}).get("products") or []

    def session_product(user, index):
        products = session_products(user)
        return products[index - 1] if 1 <= index <= len(products) else None

    def watches_of(user):
        try:
            return memory.watches(user)
        except Exception as exc:
            failure("memory_watch_read", exc)
            return None

    async def create_watch(message, user, index):
        product = session_product(user, index)
        if product is None:
            await answer_with_retry(
                message.answer,
                "В последнем ответе нет товара с таким номером. Пришлите запрос и нажмите /watch.")
            return
        if product.get("price") is None:
            await answer_with_retry(message.answer, "У этого товара нет цены — следить не за чем.")
            return
        try:
            memory.save_watch(user, product)
        except Exception as exc:
            failure("memory_watch_write", exc)
            await answer_with_retry(message.answer, "Не получилось сохранить подписку. Попробуйте позже.")
            return
        event("watch_created", site=product.get("site"))
        await answer_with_retry(
            message.answer,
            f"Слежу за «{escape(product['name'])}»: сообщу, когда увижу цену ниже "
            f"{money(product['price'])} ₽. Проверки идут при обычных запросах к магазинам.",
            parse_mode="HTML")

    async def handle_watch(message, user, text):
        parts = text.split()
        if len(parts) > 1:
            try:
                index = int(parts[1])
            except ValueError:
                index = 0
            await create_watch(message, user, index)
            return
        watches = watches_of(user)
        if watches is None:
            await answer_with_retry(message.answer, "Не удалось прочитать подписки. Попробуйте позже.")
            return
        keyboard = product_keyboard(session_products(user), "watch",
                                    skip={(w["site"], w["product_key"]) for w in watches})
        if watches:
            rows = [watch_list_text(watches), "Убрать: /unwatch N."]
            if keyboard:
                rows.append("Добавить ещё — выберите товар ниже.")
            else:
                rows.append("Все товары из последнего ответа уже под наблюдением.")
            await answer_with_retry(message.answer, "\n".join(rows), parse_mode="HTML",
                                    reply_markup=keyboard)
            return
        if keyboard:
            await answer_with_retry(
                message.answer,
                "Подписок пока нет. За каким товаром из последнего ответа следить?",
                reply_markup=keyboard)
        else:
            await answer_with_retry(
                message.answer,
                "Подписок пока нет. Пришлите товар, затем нажмите /watch — покажу номера "
                "из последнего ответа.")

    async def handle_unwatch(message, user, text):
        parts = text.split()
        watches = watches_of(user)
        if watches is None:
            await answer_with_retry(message.answer, "Не удалось прочитать подписки. Попробуйте позже.")
            return
        if len(parts) == 1:
            if not watches:
                await answer_with_retry(message.answer, "Подписок пока нет. Добавить: /watch.")
                return
            await answer_with_retry(message.answer, watch_list_text(watches), parse_mode="HTML",
                                    reply_markup=unwatch_keyboard(watches))
            return
        try:
            index = int(parts[1])
        except ValueError:
            index = 0
        if not 1 <= index <= len(watches):
            await answer_with_retry(message.answer, "Нет такой подписки. Список: /watch.")
            return
        memory.remove_watch(user, watches[index - 1]["id"])
        await answer_with_retry(message.answer, "Убрала из отслеживания.")

    async def show_history(message, user, index):
        product = session_product(user, index)
        if product is None:
            await answer_with_retry(message.answer,
                                    "В последнем ответе нет товара с таким номером. Пришлите запрос и нажмите /history.")
            return
        try:
            rows = memory.history(product.get("site"), memory.product_key(product))
            stats = memory.price_stats(product.get("site"), memory.product_key(product))
        except Exception as exc:
            failure("memory_history_read", exc)
            await answer_with_retry(message.answer, "Не удалось прочитать историю. Попробуйте позже.")
            return
        if not rows:
            await answer_with_retry(message.answer, "Пока нет истории: это первое наблюдение цены.")
            return
        lines = [f"<b>История:</b> {escape(product['name'])}"]
        for row in rows:
            when = row.get("checked_at") or "—"
            lines.append(f"• {escape(when)} — {money(row['price'])} ₽")
        if stats:
            def stamp(checked):
                return f" ({escape(checked)})" if checked else ""
            if Decimal(str(stats["min"])) == Decimal(str(stats["max"])):
                lines.append(f"Все наблюдения по одной цене: {money(stats['min'])} ₽.")
            else:
                lines.append(f"Минимум: {money(stats['min'])} ₽{stamp(stats['min_checked'])}.")
                lines.append(f"Максимум: {money(stats['max'])} ₽{stamp(stats['max_checked'])}.")
            lines.append(f"Наблюдений: {stats['observations']}.")
        await answer_with_retry(message.answer, "\n".join(lines), parse_mode="HTML")

    async def handle_history(message, user, text):
        parts = text.split()
        if len(parts) == 1:
            keyboard = product_keyboard(session_products(user), "hist")
            if keyboard:
                await answer_with_retry(
                    message.answer,
                    "За какой товар из последнего ответа показать историю?",
                    reply_markup=keyboard)
            else:
                await answer_with_retry(
                    message.answer,
                    "Сначала пришлите товар — затем нажмите /history и выберите номер.")
            return
        try:
            index = int(parts[1])
        except ValueError:
            index = 0
        await show_history(message, user, index)

    @dispatcher.message()
    async def handle(message: Message):
        if not message.from_user or not allowed(message.from_user.id, message.chat.type, ids):
            return
        user = message.from_user.id
        text = message.text or ""
        if user in captcha_waiters:
            answer = text.strip()
            if not answer:
                await answer_with_retry(message.answer,
                                        "Пришлите текст с картинки одним сообщением.")
                return
            if answer.startswith("/"):
                await answer_with_retry(
                    message.answer,
                    "Сначала закончим с капчей: пришлите текст с картинки или подождите 3 мин.")
                return
            waiter = captcha_waiters[user]
            if not waiter.done():
                waiter.set_result(answer)
            await answer_with_retry(message.answer, "Проверяю ответ…")
            return
        if text in ("/start", "/help"):
            await answer_with_retry(message.answer, "Я Penny. Ищу цены в М.Косметик, Магните, Дикси, Пятёрочке и Ленте рядом с домом. Напишите, например: «Сколько стоит зубная нить Splat?», а затем можно уточнить: «а дешевле?» или «только Splat». Недавнюю полную проверку могу использовать повторно; фраза «обнови цены» проверит магазины заново. Цены: /watch — выбрать товар из последнего ответа и следить за падением цены, /unwatch — снять подписку, /history — история цены. Если Дикси просит капчу — команда /dixy_captcha: пришлю картинку, введите текст с неё.")
            return
        if text.strip().lower() in ("/dixy_captcha", "/dixy"):
            await handle_dixy_captcha(message, user)
            return
        lowered = text.strip().lower()
        if lowered == "/watch" or lowered.startswith("/watch "):
            await handle_watch(message, user, text)
            return
        if lowered == "/unwatch" or lowered.startswith("/unwatch "):
            await handle_unwatch(message, user, text)
            return
        if lowered == "/history" or lowered.startswith("/history "):
            await handle_history(message, user, text)
            return
        if user in captcha_active:
            await answer_with_retry(
                message.answer,
                "Сейчас идёт разблокировка Дикси — дождитесь картинки с капчей или её завершения.")
            return
        if not text or len(text) > 1000:
            await answer_with_retry(message.answer, "Пришлите название товара текстом, до 1000 символов.")
            return
        if user in busy:
            event("request_rejected", message_id=message.message_id, reason="busy")
            await answer_with_retry(message.answer, "Ещё проверяю предыдущий запрос. Дождитесь ответа.")
            return
        remaining = cooldown_remaining(last.get(user), time.monotonic())
        if remaining:
            event("request_rejected", message_id=message.message_id, reason="cooldown", remaining_seconds=remaining)
            await answer_with_retry(message.answer, f"Следующий запрос можно отправить через {remaining} сек.")
            return
        busy.add(user)
        last[user] = time.monotonic()
        token = request_id.set(uuid.uuid4().hex[:12])
        event("request_received", message_id=message.message_id, characters=len(text))
        content("user_message", text)
        stage = "query"
        try:
            with span("query"):
                try:
                    previous = memory.get_session(user)
                except Exception as exc:
                    failure("memory_session_read", exc)
                    previous = None
                intent = await model.interpret(text, previous)
                query, sort, budget = intent.query, intent.sort, intent.budget
            content("search_query", query)
            event("intent_resolved", continued=intent.continued,
                  has_previous=previous is not None, refresh=intent.refresh, sort=sort)
            if intent.route == "conversation":
                answer = conversation_answer(intent.conversation_kind)
                if answer:
                    stage = "telegram"
                    await answer_with_retry(message.answer, answer)
                last.pop(user, None)
                event("conversation_answered", kind=intent.conversation_kind,
                      replied=answer is not None)
                return
            if not query:
                last.pop(user, None)
                event("query_unsupported", cooldown_cleared=True)
                await answer_with_retry(message.answer,
                                        "Не понял, какой товар искать. Напишите название, например «зубная нить Splat» или «гипоаллергенный порошок».")
                return
            if intent.route != "catalog":
                answer = session_answer(
                    intent, previous.get("products")
                    if intent.continued and previous else None)
                stage = "telegram"
                await answer_with_retry(
                    message.answer, answer, parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True))
                last.pop(user, None)  # informational follow-ups should not impose search cooldown
                if intent.continued and previous:
                    try:
                        memory.save_session(
                            user, previous["request"], previous["query"], previous["sort"],
                            previous.get("budget"), previous.get("products"),
                            turn=intent_turn(intent))
                    except Exception as exc:
                        failure("memory_session_write", exc)
                elif intent.query:
                    try:
                        memory.save_session(user, intent.request, intent.query, intent.sort,
                                            intent.budget, [], turn=intent_turn(intent))
                    except Exception as exc:
                        failure("memory_session_write", exc)
                event("session_question_answered", route=intent.route,
                      aggregation=intent.result_aggregation, info_topic=intent.info_topic)
                return
            result = None
            if not intent.refresh:
                try:
                    result = memory.cached(query, sort)
                except Exception as exc:
                    failure("memory_cache_read", exc)
            if result is not None:
                checked = datetime.fromtimestamp(
                    result["cached_at"], ZoneInfo("Europe/Moscow")).strftime("%H:%M МСК")
                result["notes"] = list(result.get("notes", [])) + [
                    f"Цены проверены в {checked}. Напишите «обнови цены», чтобы проверить заново."]
                event("search_cache_hit", age_seconds=max(0, int(time.time()) - result["cached_at"]))
            else:
                stage = "telegram_progress"
                await answer_with_retry(message.answer, "Проверяю М.Косметик, Магнит, Дикси, Пятёрочку и Ленту…")
                stage = "store"
                with span("store"):
                    result = await research(model, query, intent.request,
                                            os.environ["STORE_PROXY"], sort=sort)
                try:
                    memory.attach_history(result)  # before the insert below
                except Exception as exc:
                    failure("memory_history_read", exc)
                try:
                    memory.record_search(user, intent, result)
                except Exception as exc:
                    failure("memory_record", exc)
                try:
                    for watch, product in memory.price_drops(result.get("products", [])):
                        await send_drop_alert(message.bot, memory, watch, product)
                        memory.mark_drop(watch["id"], product["price"])
                except Exception as exc:
                    failure("watch_alert", exc)
            event("store_results", candidates=len(result["products"]))
            content("store_products", result["products"])
            stage = "selection"
            with span("selection"):
                answer, primary_products = await model.select(
                    query, result, original_request=intent.request,
                    budget=budget, with_meta=True)
            content("reply", answer)
            stage = "telegram"
            with span("telegram"):
                chunks = list(reply_chunks(answer))
                keyboard = captcha_keyboard(result)
                for index, chunk in enumerate(chunks):
                    markup = keyboard if index == len(chunks) - 1 else None
                    await answer_with_retry(message.answer, chunk, parse_mode="HTML",
                                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                                            reply_markup=markup)
            try:
                memory.save_session(user, intent.request, query, sort, budget,
                                    primary_products, turn=intent_turn(intent))
            except Exception as exc:
                failure("memory_session_write", exc)
            event("request_completed")
        except Exception as exc:
            failure(stage, exc)
            if isinstance(exc, TelegramNetworkError):
                reason = "Не удалось отправить ответ в Telegram. Проверьте связь и повторите запрос."
            elif stage == "store":
                reason = "Не удалось получить данные магазинов. Проверьте подключение к прокси и повторите запрос."
            elif stage in ("query", "selection"):
                reason = "Не удалось обработать запрос через модель. Попробуйте позже."
            else:
                reason = "Не удалось отправить результат. Попробуйте позже."
            try:
                await message.answer(reason + " Код: " + request_id.get())
            except Exception as send_error:
                failure("telegram_error_reply", send_error)
        finally:
            busy.discard(user)
            request_id.reset(token)

    @dispatcher.callback_query(F.data == "dixy_captcha")
    async def handle_captcha_button(callback):
        if (not callback.from_user or not isinstance(callback.message, Message)
                or not allowed(callback.from_user.id, callback.message.chat.type, ids)):
            await callback.answer("Сообщение устарело — отправьте /dixy_captcha.",
                                  show_alert=True)
            return
        await callback.answer()
        await handle_dixy_captcha(callback.message, callback.from_user.id)

    @dispatcher.callback_query(F.data.startswith("watch:"))
    async def handle_watch_button(callback):
        if (not callback.from_user or not isinstance(callback.message, Message)
                or not allowed(callback.from_user.id, callback.message.chat.type, ids)):
            await callback.answer("Сообщение устарело — отправьте /watch.", show_alert=True)
            return
        await callback.answer()
        try:
            index = int((callback.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            return
        await create_watch(callback.message, callback.from_user.id, index)

    @dispatcher.callback_query(F.data.startswith("unwatch:"))
    async def handle_unwatch_button(callback):
        if (not callback.from_user or not isinstance(callback.message, Message)
                or not allowed(callback.from_user.id, callback.message.chat.type, ids)):
            await callback.answer("Сообщение устарело — отправьте /unwatch.", show_alert=True)
            return
        await callback.answer()
        try:
            watch_id = int((callback.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            return
        memory.remove_watch(callback.from_user.id, watch_id)
        await callback.message.answer("Убрала из отслеживания.")

    @dispatcher.callback_query(F.data.startswith("hist:"))
    async def handle_history_button(callback):
        if (not callback.from_user or not isinstance(callback.message, Message)
                or not allowed(callback.from_user.id, callback.message.chat.type, ids)):
            await callback.answer("Сообщение устарело — отправьте /history.", show_alert=True)
            return
        await callback.answer()
        try:
            index = int((callback.data or "").split(":", 1)[1])
        except (IndexError, ValueError):
            return
        await show_history(callback.message, callback.from_user.id, index)

    try:
        async with Bot(os.environ["TELEGRAM_BOT_TOKEN"], session=telegram_session()) as bot:
            bot.session.middleware(PollingHealth())
            await wait_for_telegram(bot)
            try:
                await bot.set_my_commands(COMMANDS)
                event("bot_commands_registered", count=len(COMMANDS))
            except Exception as exc:
                event("bot_commands_failed", error=type(exc).__name__)
            event("telegram_polling_started")
            # Warm the Camoufox process so the first user request does not pay the
            # browser launch; failures here degrade browser stores gracefully.
            threading.Thread(target=_warm_browser, name="penny-warm-browser", daemon=True).start()
            watch_task = None
            try:
                interval = int(os.getenv("PENNY_WATCH_INTERVAL", "10800"))
            except ValueError:
                interval = 0
            if interval > 0:
                from .watches import watch_loop
                watch_task = asyncio.create_task(
                    watch_loop(bot, memory, os.environ["STORE_PROXY"], interval=interval))
                event("watch_loop_started", interval_seconds=interval)
            try:
                await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"],
                                               tasks_concurrency_limit=4)
            finally:
                if watch_task is not None:
                    watch_task.cancel()
                    try:
                        await watch_task
                    except (asyncio.CancelledError, Exception):
                        pass
    finally:
        try:
            from .browser import close_all_browsers
            close_all_browsers()
        except Exception as exc:
            event("browser_close_failed", error=type(exc).__name__)


def _warm_browser():
    from .browser import get_browser, run_browser_task
    for site in ("lenta", "5ka"):
        try:
            run_browser_task(get_browser, site, site=site)  # one thread per headless mode
        except Exception as exc:
            event("browser_warmup_failed", site=site, error=type(exc).__name__)
    event("browser_warmup_done")


SHUTDOWN_GRACE_S = 2.0


def main():
    """Run the bot; one Ctrl+C stops it within two seconds even mid-search."""
    stopping = threading.Event()

    def interrupt(signum, frame):
        if stopping.is_set():
            os._exit(130)  # second Ctrl+C: leave immediately
        stopping.set()
        print(f"\nPenny is stopping: closing browsers, at most {SHUTDOWN_GRACE_S:g} seconds…",
              flush=True)
        # Browser and store work run in threads that cannot be cancelled.
        # Closing them is best-effort; this timer is the hard deadline.
        timer = threading.Timer(SHUTDOWN_GRACE_S, os._exit, args=(130,))
        timer.daemon = True
        timer.start()
        event("shutdown_requested", signal=signum)
        raise KeyboardInterrupt

    try:
        import signal
        signal.signal(signal.SIGINT, interrupt)
    except (ValueError, OSError):
        pass  # not the main thread or signals unsupported; keep the default
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if stopping.is_set():
            # Do not let interpreter shutdown join a browser thread that may
            # still be inside a Playwright call; close_all_browsers already
            # had its bounded attempt in run()'s finally.
            event("shutdown_complete")
            os._exit(130)
