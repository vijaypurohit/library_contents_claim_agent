"""Shops, defined in config/locales/<country>.yaml and driven by one generic adapter.

A shop entry says how to search it and how to read the result, for example:

    - name: flipkart
      kinds: [book, item]          # what this shop is searched for
      search_url: "https://www.flipkart.com/search?q={query}"
      strategy: jsonld             # read schema.org JSON-LD on the result page
      follow: 3                    # if the result page only links to products, open this many

Strategies:
    jsonld   the search page's JSON-LD; records with offers are read directly (AbeBooks), and an
             ItemList of bare links is followed to product pages, read the same way (Flipkart)
    shopify  Shopify's /search/suggest.json for product links, then /products/<handle>.js for the
             price, tags and description (Oxford Bookstore); the product page itself is the snapshot
Adding a shop that uses one of these strategies is a YAML change only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote_plus

import yaml
from pydantic import BaseModel, Field

from claimagent.pricing.extract import (Listing, item_list_links, jsonld_blocks, listings_from_jsonld,
                                        shopify_listing, shopify_suggest_handles)
from claimagent.pricing.fetch import fetch_page

if TYPE_CHECKING:
    from claimagent.core.context import RunContext
    from claimagent.core.settings import Settings


class ShopConfig(BaseModel):
    name: str
    kinds: list[Literal["book", "item"]] = Field(default_factory=lambda: ["book"])
    search_url: str                     # "{query}" is replaced by the URL-encoded search words
    strategy: Literal["jsonld", "shopify"]
    follow: int = Field(3, ge=0, le=10)
    base_url: str | None = None         # shopify: the shop's address, for product links
    currency: str | None = None         # shopify: currency of its prices (the JSON doesn't say)
    used_source: bool = False           # this shop is searched for used copies even after new prices were found


class LocaleShops(BaseModel):
    """The pricing settings of one country."""

    country: str
    currency: str
    appraisal_threshold: float          # in `currency`; above it, a person appraises and no price is set
    shops: list[ShopConfig] = Field(default_factory=list)
    blocked: dict[str, str] = Field(default_factory=dict)      # shops known to refuse plain fetches, and why
    item_terms: dict[str, str] = Field(default_factory=dict)   # detect category -> local search word

    @classmethod
    def load(cls, settings: Settings) -> LocaleShops:
        return cls.model_validate(yaml.safe_load(settings.locale.shops_file.read_text()))


class Found(BaseModel):
    """A listing together with the page it was read from."""

    listing: Listing
    shop: str
    snapshot_ref: str
    retrieved_at: datetime


class ConfiguredShop:
    def __init__(self, ctx: RunContext, cfg: ShopConfig):
        self.ctx, self.cfg = ctx, cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    def search(self, query: str, relevant: Callable[[str], bool]) -> list[Found]:
        """Listings for a query. `relevant(name)` decides which linked products are worth opening.
        Raises ShopBlocked when the shop refuses the search."""
        url = self.cfg.search_url.format(query=quote_plus(query))
        page = fetch_page(self.ctx, url)
        if self.cfg.strategy == "shopify":
            return self._shopify(json.loads(page.text), relevant)
        blocks = jsonld_blocks(page.text)
        found = [self._found(l, page) for l in listings_from_jsonld(blocks, url)]
        if found:
            return found
        links = [link for name, link in item_list_links(blocks) if relevant(name)][:self.cfg.follow]
        for link in links:
            product = fetch_page(self.ctx, link)
            found += [self._found(l, product) for l in listings_from_jsonld(jsonld_blocks(product.text), link)]
        return found

    def _shopify(self, suggest: dict, relevant: Callable[[str], bool]) -> list[Found]:
        found = []
        for title, path in shopify_suggest_handles(suggest)[:self.cfg.follow * 2]:
            if not relevant(title) or len(found) >= self.cfg.follow:
                continue
            product_url = f"{self.cfg.base_url}{path}"
            data = fetch_page(self.ctx, f"{product_url}.js")
            listing = shopify_listing(json.loads(data.text), product_url, self.cfg.currency or self.ctx.settings.locale.currency)
            if listing is not None:
                page = fetch_page(self.ctx, product_url)  # the human-readable page is the evidence
                found.append(self._found(listing, page))
        return found

    def _found(self, listing: Listing, page) -> Found:
        return Found(listing=listing, shop=self.cfg.name, snapshot_ref=page.snapshot_ref, retrieved_at=page.retrieved_at)
