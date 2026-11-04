"""Telegram HTML replies built from validated catalogue products."""
import re
from decimal import Decimal
from html import escape

from .products import ordered_sections, unit_price
from .store_config import SITES
from .contracts import SearchResult, Section
from collections.abc import Sequence


def money(value):
    return f"{Decimal(value):.2f}".replace('.', ',')


def product_line(product, number=None):
    name = product["name"]
    # Move only a clear trailing package size; preserve ambiguous/multipack names.
    package = re.search(r"\s+(\d+(?:[.,]\d+)?\s*(?:кг|г|мл|л|м|шт))\s*$", name, re.I)
    if package and not re.search(r"\d\s*[×xх*]|[×xх*]\s*\d", name):
        title = name[:package.start()]
        size = re.sub(r"(\d)\s*([а-я]+)", r"\1 \2", package.group(1)).replace('.', ',')
    else:
        title, size = name, None
    prefix = f"{number}. " if number is not None else ""
    lines = [f'<b>{prefix}{escape(title)}</b>']
    if size:
        lines.append("📦 " + escape(size))
    price = f'<b>{money(product["price"])} ₽</b>' if product["price"] is not None else "цена не указана"
    price += " по карте" if product["card"] else ""
    if product.get("old_price") and product.get("old_price") != product.get("price"):
        old = f'{money(product["old_price"])} ₽'
        if product.get("discount"):
            old += f' −{int(product["discount"])}%'
        price += f" ({old})"
    unit = unit_price(product)
    if unit:
        price += f" · {money(unit[0])} ₽/{unit[1]}"
    washes = re.findall(r"\b(\d+)\s*стир(?:ок|ки|ка)\b", name.lower())
    if len(washes) == 1 and int(washes[0]) > 0 and product['price'] is not None:
        price += f" · {money(Decimal(product['price']) / int(washes[0]))} ₽/стирку"
    lines.append(price)
    previous = product.get("previous_price")
    if previous and product["price"] is not None and previous != product["price"]:
        try:
            cheaper = Decimal(str(previous)) > Decimal(str(product["price"]))
        except Exception:
            cheaper = False
        lines.append(f"{'↘' if cheaper else '↗'} было {money(previous)} ₽")
    quantity = product["quantity"]
    if quantity is None:
        lines.append("? наличие неизвестно")
    elif quantity <= 0:
        lines.append("✕ нет в наличии")
    # In stock: no line — every shown item is available, out-of-stock is filtered out before rendering.
    if product.get("store"):
        lines.append("📍 " + escape(product["store"]))
    return "\n".join(lines)


def footer(result, shown=None):
    """Per-store link for stores that actually yielded a shown product.
    shown: list of shown products; None means fall back to all candidates."""
    if shown is None:
        shown = result.get("products", [])
    counts = {}
    for product in shown:
        if product.get("store"):
            counts[product["store"]] = counts.get(product["store"], 0) + 1
    if not shown and not result.get("products"):
        counts = None  # nothing found anywhere: keep every store link for manual retry
    sites = result.get("sites") or ([result["site"]] if result.get("site") else [])
    lines = []
    for site in sites:
        cfg = SITES.get(site)
        url = (result.get("site_urls") or {}).get(site) or result.get("url")
        if not cfg or not url or (counts is not None and counts.get(cfg["name"], 0) == 0):
            continue
        lines.append((cfg, url))
    return "\n\n".join(
        f"📍 {cfg['name']}" + (f" · {cfg['address']}" if cfg.get("address") else "") + "\n"
        f'<a href="{escape(url, quote=True)}">{cfg["name"] if len(lines) > 1 else "Открыть в магазине"}</a>'
        for cfg, url in lines)


def notes_block(result):
    rows = [f"<i>{escape(note)}</i>" for note in result.get("notes", [])]
    if result.get("incomplete") and not result.get("notes"):
        rows.append("Часть проверок не удалась — результаты неполные.")
    return rows


def _unique_names(products, limit=2):
    seen, names = set(), []
    for product in products:
        key = product["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(escape(product["name"]))
        if len(names) == limit:
            break
    return names


def best_value_advice(products, sort="popularity"):
    """One code-rendered tip: the best deal among the shown cards.

    The model never writes prices; this line compares validated values. Unit
    prices are used when every shown product has the same unambiguous unit,
    otherwise the package price. It describes the shown results only."""
    priced = [p for p in products if p.get("price") is not None]
    if len(priced) < 2:
        return None
    reverse = sort == "price_desc"
    def shown(value):
        return Decimal(str(value)).quantize(Decimal("0.01"))

    with_units = [(p, unit_price(p)) for p in priced]
    if all(unit for _, unit in with_units) and len({u[1] for _, u in with_units}) == 1:
        unit = with_units[0][1][1]
        values = [shown(value) for _, (value, _) in with_units]
        best_value = max(values) if reverse else min(values)
        winners = [p for p, (value, _) in with_units if shown(value) == best_value]
        if len(winners) > 1:
            label = "Одинаково дорого" if reverse else "Одинаково выгодно"
            return (f"<b>{label} за {unit} среди показанных:</b> "
                    + " и ".join(_unique_names(winners)) + ".")
        product = winners[0]
        price = money(product["price"]) + " ₽" + (" по карте" if product.get("card") else "")
        store = f" · {escape(product['store'])}" if product.get("store") else ""
        label = "Дороже за" if reverse else "Выгоднее за"
        return (f"<b>{label} {unit} среди показанных:</b> {escape(product['name'])} — "
                f"{price} ({money(best_value)} ₽/{unit}){store}.")
    prices = [shown(p["price"]) for p in priced]
    best_price = max(prices) if reverse else min(prices)
    winners = [p for p in priced if shown(p["price"]) == best_price]
    if len(winners) > 1:
        return ("<b>Одинаковая цена среди показанных:</b> "
                + " и ".join(_unique_names(winners)) + f" — {money(best_price)} ₽.")
    best = winners[0]
    price = money(best["price"]) + " ₽" + (" по карте" if best.get("card") else "")
    store = f" · {escape(best['store'])}" if best.get("store") else ""
    label = "Дороже всего" if reverse else "Дешевле всего"
    return f"<b>{label} среди показанных:</b> {escape(best['name'])} — {price}{store}."


def render_sections(result: SearchResult, sections: Sequence[Section],
                    conclusion: str = "", strict: bool = False) -> str:
    """Format selected sections without modifying the result or selection."""
    rows = []
    shown = []
    for section in ordered_sections(result["products"], sections, result.get("sort")):
        items = section["items"]
        rows.append(f"<b>{escape(section['title'])}</b>")
        shown.extend(result["products"][i] for i in section["items"])
        rows.extend(product_line(result["products"][i], n) for n, i in enumerate(items, 1))
        if section.get("note"):
            rows.append(escape(section["note"]))
    if conclusion:
        rows.append(f"<i>{escape(conclusion)}</i>")
    advice = best_value_advice(shown, result.get("sort", "popularity"))
    if advice:
        rows.append(advice)
    rows.extend(notes_block(result))
    rows.append(footer(result, shown))
    return "\n\n".join(rows)


def render(result, exact, alternatives=(), different_format=(), strict=False):
    if exact:
        rows = ["<b>Нашла по вашему запросу:</b>"]
        comparable = [result['products'][i] for i in exact]
        units = [unit_price(p) for p in comparable]
        if (len(comparable) > 1 and all(units)
                and len({u[1] for u in units}) == 1
                and len({p['card'] for p in comparable}) == 1
                and all(p['quantity'] is not None and p['quantity'] > 0 for p in comparable)
                and not any('концентр' in p['name'].lower() for p in comparable)):
            exact = sorted(exact, key=lambda i: unit_price(result['products'][i])[0])
            best = unit_price(result['products'][exact[0]])
            tied = all(unit_price(result['products'][i])[0] == best[0] for i in exact)
            rows = [f"<b>Одинаковая цена — {money(best[0])} ₽/{best[1]}.</b>" if tied else
                    f"<b>Первый вариант выгоднее за {best[1]} среди найденных.</b>"]
        rows.extend(product_line(result["products"][i], n) for n, i in enumerate(exact[:5], 1))
    elif not result["products"]:
        rows = ["Ни один магазин не вернул товаров по этому запросу. Попробуйте другое название или бренд."
                if len(result.get("sites", [])) > 1 else
                "Магазин не вернул товаров по этому запросу. Попробуйте другое название или бренд."]
    else:
        rows = ["Точного совпадения в результатах поиска не нашла."]
        if strict:
            rows.append("Другие бренды и форматы не предлагаю: вы попросили без замен.")
        else:
            if alternatives:
                rows.append("<b>Альтернативы других брендов:</b>")
                rows.extend(product_line(result["products"][i], n) for n, i in enumerate(alternatives[:3], 1))
            if different_format:
                rows.append("<b>Другой формат для той же задачи:</b>")
                rows.append(product_line(result["products"][different_format[0]]))
            if not alternatives and not different_format:
                rows.append("Подходящих альтернатив тоже не нашла. Попробуйте уточнить название.")
    shown = [result["products"][i] for i in exact[:5]] if exact else []
    if not exact and result["products"]:
        shown = [result["products"][i] for i in list(alternatives[:3]) + list(different_format[:1])]
    rows.extend(notes_block(result))
    rows.append(footer(result, shown))
    return "\n\n".join(rows)


def reply_chunks(answer, limit=3500):
    """Split between complete HTML paragraphs, never inside a tag or entity."""
    chunk = ""
    for paragraph in answer.split("\n\n"):
        if len(paragraph) > limit:
            # Rendered product paragraphs are bounded; fail safely for unexpected input.
            raise ValueError("Reply paragraph too long")
        if chunk and len(chunk) + len(paragraph) + 2 > limit:
            yield chunk
            chunk = ""
        chunk = chunk + "\n\n" + paragraph if chunk else paragraph
    if chunk:
        yield chunk
