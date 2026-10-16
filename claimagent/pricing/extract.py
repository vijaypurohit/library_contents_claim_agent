"""Reading listings (title, author, price, condition) out of shop pages. Pure functions: text in, listings out.

Strategies, in order of preference:
- JSON-LD: the schema.org data shops embed for search engines (<script type="application/ld+json">).
  It holds Book or Product records with an `offers` block (price, currency, condition). A search
  page may embed full records (AbeBooks) or only a list of product links (Flipkart), which are then
  followed to their product pages.
- Shopify JSON: Shopify shops answer /search/suggest.json and /products/<handle>.js with clean JSON.
Prices are never read from free text: a number counts as a price only inside one of these structures.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel

Condition = Literal["new", "used"]
_LD_SCRIPT = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


class Listing(BaseModel):
    """One offer for one product on one shop page."""

    title: str
    url: str
    price: float
    currency: str
    condition: Condition
    authors: list[str] = []
    author_text: str = ""        # free text that may name the author (description, tags)
    publisher: str | None = None
    isbn: str | None = None
    list_price: float | None = None   # printed price (MRP) when the shop shows it
    available: bool = True            # False: listed but sold out (its price still shows the shop's price)


def jsonld_blocks(html: str) -> list[dict[str, Any]]:
    """Every JSON-LD object on a page, with lists and @graph containers flattened."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, dict):
            found.append(node)
            if "@graph" in node:
                walk(node["@graph"])

    for match in _LD_SCRIPT.finditer(html):
        try:
            walk(json.loads(html_lib.unescape(match.group(1)).strip()))
        except json.JSONDecodeError:
            continue  # one broken block must not hide the others
    return found


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    return set(t if isinstance(t, list) else [t])


def _names(value: Any) -> list[str]:
    """Author or brand field as a list of names (it may be a string, an object, or a list of either)."""
    items = value if isinstance(value, list) else [value]
    names = []
    for item in items:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _condition(offer: dict) -> Condition:
    return "used" if "used" in str(offer.get("itemCondition", "")).lower() else "new"


def _isbn(record: dict) -> str | None:
    """The record's ISBN; shops such as Flipkart put it in `sku` instead."""
    for value in (record.get("isbn"), record.get("sku")):
        digits = str(value or "").replace("-", "")
        if len(digits) in (10, 13) and digits[:-1].isdigit():
            return digits
    return None


def listings_from_jsonld(blocks: list[dict[str, Any]], page_url: str) -> list[Listing]:
    """Listings from Book/Product records that carry a priced offer, including ones inside an ItemList."""
    records = []
    for block in blocks:
        if _types(block) & {"Book", "Product"}:
            records.append(block)
        if "ItemList" in _types(block):
            for element in block.get("itemListElement", []):
                item = element.get("item") if isinstance(element, dict) else None
                if isinstance(item, dict) and _types(item) & {"Book", "Product"}:
                    records.append(item)

    listings = []
    for record in records:
        offers = record.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict) or offer.get("price") in (None, ""):
                continue
            try:
                price = float(str(offer["price"]).replace(",", ""))
            except ValueError:
                continue
            if price <= 0 or not offer.get("priceCurrency"):
                continue
            availability = str(offer.get("availability", "")).lower()
            listings.append(Listing(
                available=not any(word in availability for word in ("outofstock", "soldout", "discontinued")),
                title=str(record.get("name", "")).strip(), url=offer.get("url") or record.get("url") or page_url,
                price=price, currency=str(offer["priceCurrency"]).upper(), condition=_condition(offer),
                authors=_names(record.get("author")), author_text=_TAGS.sub(" ", str(record.get("description", ""))),
                publisher=next(iter(_names(record.get("publisher")) + _names(record.get("brand"))), None),
                isbn=_isbn(record),
            ))
    return listings


def item_list_links(blocks: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(name, url) of products an ItemList only links to, in page order, without repeats."""
    links, seen = [], set()
    for block in blocks:
        if "ItemList" not in _types(block):
            continue
        for element in block.get("itemListElement", []):
            if not isinstance(element, dict) or isinstance(element.get("item"), dict):
                continue  # full records are read by listings_from_jsonld
            url, name = element.get("url"), element.get("name", "")
            if url and url not in seen:
                seen.add(url)
                links.append((name, url))
    return links


def shopify_suggest_handles(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(title, product path without query) from a Shopify /search/suggest.json answer."""
    products = data.get("resources", {}).get("results", {}).get("products", [])
    return [(p.get("title", ""), p["url"].split("?")[0]) for p in products if p.get("url")]


def shopify_listing(product: dict[str, Any], url: str, currency: str) -> Listing | None:
    """A listing from a Shopify /products/<handle>.js answer (prices there are in minor units)."""
    if not product.get("price"):
        return None
    tags = " ".join(product.get("tags") or [])
    return Listing(title=product.get("title", ""), url=url, price=product["price"] / 100, currency=currency,
                   condition="new", author_text=f"{tags} {_TAGS.sub(' ', product.get('description') or '')}",
                   list_price=(product.get("compare_at_price") or 0) / 100 or None, available=bool(product.get("available")))
