"""Pure product calculations, independent of store transports and Telegram."""
import re
from decimal import Decimal
from collections.abc import Sequence

from .contracts import Product, Section


def unit_price(product):
    # Ambiguous multipacks and multiple size mentions deliberately remain unparsed.
    name = product['name'].lower()
    matches = re.findall(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л)\b", name)
    if len(matches) != 1 or re.search(r"\d\s*[×xх*]|[×xх*]\s*\d", name) or product['price'] is None:
        return None
    amount, unit = matches[0]
    amount = Decimal(amount.replace(',', '.'))
    if amount <= 0:
        return None
    if unit in ('г', 'мл'):
        amount /= 1000
    return Decimal(product['price']) / amount, ('кг' if unit in ('г', 'кг') else 'л')


def available(product):
    """Out-of-stock items are hidden from replies; unknown stock still shows."""
    quantity = product.get("quantity")
    return not (type(quantity) in (int, float) and quantity <= 0)


def ordered_sections(products: Sequence[Product], sections: Sequence[Section],
                     sort: str | None = None) -> list[Section]:
    """Return section copies in display order without changing the selection.

    Explicit price order applies within each section, with unknown prices last.
    Otherwise only a single section of comparable, in-stock products is ordered
    by unit price. Stable sorting preserves the selected order for ties.
    """
    ordered = []
    for section in sections:
        items = list(section["items"])
        if sort in ("price_asc", "price_desc"):
            priced = [i for i in items if products[i].get("price") is not None]
            unpriced = [i for i in items if products[i].get("price") is None]
            items = sorted(priced, key=lambda i: Decimal(products[i]["price"]),
                           reverse=sort == "price_desc") + unpriced
        elif len(sections) == 1 and len(items) > 1:
            comparable = [products[i] for i in items]
            units = [unit_price(p) for p in comparable]
            if (all(units) and len({u[1] for u in units}) == 1
                    and len({p['card'] for p in comparable}) == 1
                    and all(p['quantity'] is not None and p['quantity'] > 0 for p in comparable)
                    and not any('концентр' in p['name'].lower() for p in comparable)):
                items = sorted(items, key=lambda i: unit_price(products[i])[0])
        ordered.append({**section, "items": items})
    return ordered
