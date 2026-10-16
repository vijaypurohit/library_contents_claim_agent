"""Stage 7: Price. Replacement cost and used value of every book, and a price range for every item.

Books (identified or read from the spine; unreadable books are not priced):
  1. Search the country's shops (config/locales/<country>.yaml) with title and author; when a shop
     finds nothing, again with the spine's own wording (shops may write "Class XI" where the
     catalogue wrote "Class 11"), then with the title alone. A book stops after two shops return a
     matching new price; shops marked `used_source` are still searched.
  2. A listing counts only if it is the same book: title and author agree (text/match.py), and any
     class or part number on the spine appears in the listing. When the spine or the listing names
     no author, the whole listing title (bracketed edition notes aside) must almost equal a title
     of at least three meaningful words, so "Physics" never matches "Physics: Concepts and
     Connections"; the quote is marked `title_only` and the book goes to review. A listing whose
     author disagrees never counts.
  3. Replacement cost = median of the new prices. Used value = median of the used prices, capped at
     the replacement cost. A book without a used listing, or without a new price to cap against,
     has no used value (left out of that total).
     Sold-out listings count: their price is still what the shop charges.
  4. Prices in the claim's currency are preferred. Only when a condition (new or used) has none are
     foreign prices converted at the day's reference rate; those quotes are marked `converted`.
  5. Above the country's appraisal threshold (on the replacement cost), no price is set: a person appraises.
Items: the same shops (those that list items), searched with the item's local name and material;
the 25th-75th percentile of matching prices is the range.

Every quote keeps its URL, retrieval time and a snapshot of the page (evidence/prices/).
Shops that refuse requests are listed in `blocked_shops` and in the stage warnings.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.pricing.extract import Listing
from claimagent.pricing.fetch import ShopBlocked
from claimagent.pricing.fx import RateUnavailable, rate
from claimagent.pricing.shop import ConfiguredShop, Found, LocaleShops
from claimagent.stages.identify import IdentifiedBook, IdentifyOutput
from claimagent.stages.inventory import InventoryItem, InventoryOutput
from claimagent.text.match import (book_score, core_title, level_numbers, normalise, normalise_name, numbers_agree,
                                   title_score)

QUOTE_MATCH = 85.0           # book_score a listing needs when both name an author (80 let "Pharmaceutical Inorganic Chemistry" through)
TITLE_ONLY_MATCH = 92.0      # title similarity a listing needs, when the spine shows no author
RELEVANT_TITLE = 55.0        # a linked product is opened only if its name is at least this similar
NEW_SHOPS_ENOUGH = 2
PARALLEL_BOOKS = 4
ITEM_WORDS_NEEDED = 0.5      # share of the query's words an item listing's title must contain
MAX_QUOTES = 5               # per shop and condition: enough for a median, short enough to check by hand
_TITLE_SEPARATORS = (" - ", " – ", ":", "(", "|", ",")
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
TITLE_ONLY_WORDS = 3         # a title matched without an author needs at least this many meaningful words

BookStatus = Literal["priced", "no_quotes", "needs_appraisal", "not_priced"]
ItemStatus = Literal["range", "no_quotes", "needs_appraisal"]


class Quote(BaseModel):
    shop: str
    url: str
    title: str
    condition: Literal["new", "used"]
    price: float                     # as listed
    currency: str
    price_local: float               # in the claim's currency
    converted: bool = False
    fx_rate: float | None = None
    available: bool = True           # False: sold out at that shop
    match_score: float
    title_only: bool = False
    retrieved_at: datetime
    snapshot_ref: str


class BookPrice(BaseModel):
    book_id: str
    status: BookStatus
    currency: str
    replacement_cost: float | None = None
    used_value: float | None = None
    quotes: list[Quote] = Field(default_factory=list)
    review: list[str] = Field(default_factory=list)


class ItemPrice(BaseModel):
    item_id: str
    status: ItemStatus
    currency: str
    query: str
    low: float | None = None
    high: float | None = None
    quotes: list[Quote] = Field(default_factory=list)
    review: list[str] = Field(default_factory=list)


class PriceOutput(BaseModel):
    country: str
    currency: str
    books: list[BookPrice]
    items: list[ItemPrice]
    blocked_shops: dict[str, str]    # shop -> reason, including those blocked during this run


# ---- Matching and arithmetic (pure) ----

def title_variants(title: str) -> list[str]:
    """A listing title, and its main part before a subtitle or edition note."""
    variants = [title]
    for sep in _TITLE_SEPARATORS:
        head = title.split(sep)[0].strip()
        if head and head not in variants:
            variants.append(head)
    return variants


def book_titles(book: IdentifiedBook) -> list[str]:
    """Ways a shop may list the book: its title, with the spine's brand in front ("Pradeep's
    Fundamental Physics"), and with the class printed elsewhere on the spine ("... Class 12")."""
    titles = [book.title or ""]
    if book.publisher and normalise(book.publisher) not in normalise(book.title):
        titles.append(f"{book.publisher} {book.title}")
    missing = level_numbers(book.spine_text) - level_numbers(book.title)
    if len(missing) == 1:
        titles += [f"{t} class {next(iter(missing))}" for t in list(titles)]
    return titles


def match_listing(book: IdentifiedBook, listing: Listing) -> tuple[float, bool] | None:
    """(score, title_only) if the listing is this book, else None."""
    authors = listing.authors
    if not authors and book.author:
        # Some shops only name the author in a description or tags: accept a surname found there.
        surnames = {name.split()[-1] for name in (normalise_name(a) for a in book.author.split(",")) if name}
        if surnames & set(normalise(listing.author_text).split()):
            authors = [book.author]
    pairs = [(t, v) for t in book_titles(book) for v in title_variants(listing.title)]
    if book.author and authors:
        score = max(book_score(t, book.author, v, authors, book.spine_text) for t, v in pairs)
        return (round(score, 1), False) if score >= QUOTE_MATCH else None
    # One side names no author: a specific title must match the listing's whole title, numbers included.
    if len(core_title(book.title).split()) < TITLE_ONLY_WORDS:
        return None
    whole = _BRACKETS.sub(" ", listing.title)
    score = max(title_score(t, whole) for t in book_titles(book))
    agree = numbers_agree(f"{book.title} / {book.spine_text}", listing.title)
    return (round(score, 1), True) if score >= TITLE_ONLY_MATCH and agree else None


def prefer_local(quotes: list[Quote]) -> list[Quote]:
    """Per condition, drop converted quotes when quotes in the claim's own currency exist."""
    kept = []
    for condition in ("new", "used"):
        same = [q for q in quotes if q.condition == condition]
        local = [q for q in same if not q.converted]
        kept += local or same
    return kept


def book_values(new: list[float], used: list[float]) -> tuple[float | None, float | None]:
    """Replacement cost (median new) and used value (median used, capped at the replacement cost;
    none without a replacement cost)."""
    replacement = round(statistics.median(new), 2) if new else None
    if not used or replacement is None:
        return replacement, None   # without a new price the cap can't be applied, so no used value
    return replacement, min(round(statistics.median(used), 2), replacement)


def price_range(values: list[float]) -> tuple[float, float] | None:
    """25th-75th percentile of prices; the whole span when there are fewer than four."""
    if not values:
        return None
    values = sorted(values)
    if len(values) < 4:
        return values[0], values[-1]
    q = statistics.quantiles(values, n=4, method="inclusive")  # 25th and 75th percentile of the listed prices
    return round(q[0], 2), round(q[2], 2)


def item_query(item: InventoryItem, terms: dict[str, str]) -> str:
    """Search words for an item: material and the local word for its category (e.g. "wood almirah")."""
    noun = terms.get(item.category) or item.label
    words = [item.material, noun] if item.material and item.material.lower() not in noun.lower() else [noun]
    return " ".join(w for w in words if w)


def item_matches(query: str, listing_title: str) -> bool:
    wanted = set(normalise(query).split())
    have = set(normalise(listing_title).split())
    return bool(wanted) and len(wanted & have) / len(wanted) >= ITEM_WORDS_NEEDED


# ---- Stage ----

@register
class PriceStage(Stage):
    name = "price"
    order = 7
    needs = ("inventory", "identify")
    output = PriceOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> PriceOutput:
        inventory: InventoryOutput = inputs["inventory"]  # type: ignore[assignment]
        identify: IdentifyOutput = inputs["identify"]  # type: ignore[assignment]
        locale = LocaleShops.load(ctx.settings)
        pricer = Pricer(ctx, locale)
        with ThreadPoolExecutor(PARALLEL_BOOKS) as pool:
            books = list(pool.map(pricer.price_book, identify.books))
        items = [pricer.price_item(item) for item in inventory.items]
        for shop, reason in pricer.blocked.items():
            ctx.warn(f"price: shop {shop} unavailable ({reason})")
        return PriceOutput(country=locale.country, currency=locale.currency, books=books, items=items,
                           blocked_shops={**locale.blocked, **pricer.blocked})


class Pricer:
    """Shared state of one price run: the shops, currency rates and shops that failed."""

    def __init__(self, ctx: RunContext, locale: LocaleShops):
        self.ctx, self.locale = ctx, locale
        self.shops = [ConfiguredShop(ctx, cfg) for cfg in locale.shops]
        self.blocked: dict[str, str] = {}
        self._rates: dict[str, float | None] = {}

    def _search(self, shop: ConfiguredShop, query: str, relevant) -> list[Found]:
        if shop.name in self.blocked:
            return []
        try:
            return shop.search(query, relevant)
        except ShopBlocked as exc:
            self.blocked[shop.name] = str(exc)
            return []

    def _local(self, listing: Listing) -> tuple[float, float | None] | None:
        """(price in the claim currency, rate used or None), or None when no rate is available."""
        if listing.currency == self.locale.currency:
            return listing.price, None
        if listing.currency not in self._rates:
            try:
                self._rates[listing.currency] = rate(self.ctx, listing.currency, self.locale.currency).value
            except RateUnavailable as exc:
                self.ctx.warn(f"price: {exc}")
                self._rates[listing.currency] = None
        fx = self._rates[listing.currency]
        return (round(listing.price * fx, 2), fx) if fx else None

    def _quote(self, found: Found, score: float, title_only: bool) -> Quote | None:
        local = self._local(found.listing)
        if local is None:
            return None
        listing = found.listing
        return Quote(shop=found.shop, url=listing.url, title=listing.title, condition=listing.condition,
                     price=listing.price, currency=listing.currency, price_local=local[0], converted=local[1] is not None,
                     fx_rate=local[1], available=listing.available, match_score=score, title_only=title_only,
                     retrieved_at=found.retrieved_at, snapshot_ref=found.snapshot_ref)

    def price_book(self, book: IdentifiedBook) -> BookPrice:
        currency = self.locale.currency
        if book.status == "unidentified" or not book.title:
            return BookPrice(book_id=book.book_id, status="not_priced", currency=currency,
                             review=["not priced: the book could not be read"])
        first_author = normalise_name((book.author or "").split(",")[0]).split()
        surname = first_author[-1] if first_author else ""
        queries = list(dict.fromkeys(q for q in (f"{book.title} {surname}".strip(),
                                                  book.spine_text.replace(" / ", " ").strip(), book.title) if q))
        spine = f"{book.title} / {book.spine_text}"

        def relevant(name: str) -> bool:
            """Worth opening: a similar title, and no class or part number that contradicts the spine."""
            similar = max(title_score(t, v) for t in book_titles(book) for v in title_variants(name)) >= RELEVANT_TITLE
            return similar and numbers_agree(spine, name)

        quotes: list[Quote] = []
        new_shops = 0
        for shop in (s for s in self.shops if "book" in s.cfg.kinds):
            if new_shops >= NEW_SHOPS_ENOUGH and not shop.cfg.used_source:
                continue
            matched_new = False
            kept = {"new": 0, "used": 0}
            for query in queries:
                for found in self._search(shop, query, relevant):
                    match = match_listing(book, found.listing)
                    if match is None or kept[found.listing.condition] >= MAX_QUOTES:
                        continue
                    quote = self._quote(found, *match)
                    if quote is not None:
                        quotes.append(quote)
                        kept[quote.condition] += 1
                        matched_new |= quote.condition == "new"
                if sum(kept.values()):
                    break          # this query found the book here; no need for broader ones
            new_shops += matched_new

        quotes = prefer_local(quotes)
        new = [q.price_local for q in quotes if q.condition == "new"]
        used = [q.price_local for q in quotes if q.condition == "used"]
        replacement, used_value = book_values(new, used)
        review = []
        if any(q.title_only for q in quotes):
            review.append("matched on title only (no author on the spine): check the listings")
        if any(q.converted for q in quotes if q.condition == "new") and not any(not q.converted for q in quotes if q.condition == "new"):
            review.append("only foreign-currency new prices: converted")
        if used and replacement is not None and statistics.median(used) > replacement:
            review.append("used listings cost more than new ones: used value capped at the replacement cost")
        if used and replacement is None:
            review.append("used listings only: no new price to cap them against, so no used value is set")
        if not quotes:
            return BookPrice(book_id=book.book_id, status="no_quotes", currency=currency,
                             review=["no matching listing in this country's shops: excluded from totals"])
        if replacement is not None and replacement > self.locale.appraisal_threshold:
            return BookPrice(book_id=book.book_id, status="needs_appraisal", currency=currency, quotes=quotes,
                             review=[f"above the appraisal threshold of {self.locale.appraisal_threshold:g} {currency}"])
        return BookPrice(book_id=book.book_id, status="priced", currency=currency, replacement_cost=replacement,
                         used_value=used_value, quotes=quotes, review=review)

    def price_item(self, item: InventoryItem) -> ItemPrice:
        currency = self.locale.currency
        query = item_query(item, self.locale.item_terms)
        if item.category == "art":
            return ItemPrice(item_id=item.item_id, status="needs_appraisal", currency=currency, query=query,
                             review=["art is appraised by a person unless it is a print"])
        quotes = []
        for shop in (s for s in self.shops if "item" in s.cfg.kinds):
            for found in self._search(shop, query, lambda name: item_matches(query, name)):
                if item_matches(query, found.listing.title) and found.listing.condition == "new":
                    quote = self._quote(found, 100.0, True)
                    if quote is not None:
                        quotes.append(quote)
        span = price_range([q.price_local for q in quotes])
        if span is None:
            return ItemPrice(item_id=item.item_id, status="no_quotes", currency=currency, query=query,
                             review=["no comparable listing found: excluded from totals"])
        review = [f"price range from {len(quotes)} comparable listing(s) for '{query}', not this exact item"]
        if span[1] > self.locale.appraisal_threshold:
            review.append(f"upper price above the appraisal threshold of {self.locale.appraisal_threshold:g} {currency}")
        return ItemPrice(item_id=item.item_id, status="range", currency=currency, query=query, low=span[0], high=span[1],
                         quotes=quotes, review=review)
