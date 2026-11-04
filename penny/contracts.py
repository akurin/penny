"""Shared catalogue data shapes at search, selection, and rendering boundaries."""
from typing import NotRequired, TypedDict


class Product(TypedDict):
    name: str
    site: str
    store: str
    price: str | None
    quantity: int | float | None
    card: bool
    id: NotRequired[str | int | None]
    old_price: NotRequired[str | None]
    discount: NotRequired[int | float | None]
    checked: NotRequired[str]
    source: NotRequired[str]
    previous_price: NotRequired[str | None]


class Section(TypedDict):
    title: str
    items: list[int]
    note: NotRequired[str]


class SearchResult(TypedDict):
    products: list[Product]
    sites: NotRequired[list[str]]
    site_urls: NotRequired[dict[str, str]]
    url: NotRequired[str]
    site: NotRequired[str]
    checked: NotRequired[str]
    notes: NotRequired[list[str]]
    incomplete: NotRequired[bool]
    sort: NotRequired[str]
