"""Price stage: page extraction, listing matching, price arithmetic and shop policy. No network."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from claimagent.core.context import RunContext
from claimagent.domain.facts import UserFact
from claimagent.domain.records import BBox
from claimagent.pricing import fetch as fetch_module
from claimagent.pricing.extract import (Listing, item_list_links, jsonld_blocks, listings_from_jsonld, shopify_listing,
                                        shopify_suggest_handles)
from claimagent.pricing.fetch import Page, ShopBlocked, fetch_page
from claimagent.pricing.shop import ConfiguredShop, Found, LocaleShops, ShopConfig
from claimagent.stages.identify import IdentifiedBook
from claimagent.stages.inventory import InventoryItem
from claimagent.stages.price import (Pricer, print_items, Quote, book_values, item_matches, item_query, match_listing, prefer_local,
                                     price_range, title_variants)

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def ld(obj) -> str:
    return f'<html><script type="application/ld+json">{json.dumps(obj)}</script></html>'


def ident(title, author=None, spine=None, publisher=None, status="identified") -> IdentifiedBook:
    return IdentifiedBook(book_id="A1-1", frame_ref="f.jpg", crop_ref="x", spine_text=spine or title or "", legible=True,
                          status=status, title=title, author=author, publisher=publisher)


def listing(title, price=100.0, currency="INR", condition="new", authors=(), author_text="") -> Listing:
    return Listing(title=title, url=f"https://shop.example/{abs(hash(title))}", price=price, currency=currency,
                   condition=condition, authors=list(authors), author_text=author_text)


# ---- Extraction ----

ABEBOOKS_PAGE = ld({"@type": "ItemList", "itemListElement": [
    {"@type": "ListItem", "item": {"@type": "Book", "name": "Word Power Made Easy", "isbn": "9781101873854",
                                   "author": {"@type": "Person", "name": "Lewis, Norman"},
                                   "publisher": {"name": "Vintage"},
                                   "offers": {"price": 6.49, "priceCurrency": "USD", "url": "https://abe/1",
                                              "itemCondition": "https://schema.org/UsedCondition"}}}]})


def test_search_page_with_full_records():
    [found] = listings_from_jsonld(jsonld_blocks(ABEBOOKS_PAGE), "https://abe/search")
    assert (found.price, found.currency, found.condition) == (6.49, "USD", "used")
    assert found.authors == ["Lewis, Norman"] and found.publisher == "Vintage" and found.isbn == "9781101873854"


def test_product_page_with_sku_isbn_brand_and_stock():
    page = ld([{"@type": "Product", "name": "Word Power Made Easy", "sku": "9780143424680",
                "brand": {"name": "Penguin"}, "description": "by Lewis Norman from Flipkart.com",
                "offers": {"price": 182, "priceCurrency": "INR", "availability": "https://schema.org/OutOfStock"}}])
    [found] = listings_from_jsonld(jsonld_blocks(page), "https://fk/p")
    assert found.isbn == "9780143424680" and found.publisher == "Penguin" and "Lewis" in found.author_text
    assert not found.available and found.url == "https://fk/p"


def test_link_only_item_list_and_broken_blocks():
    page = ld({"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "name": "A", "url": "https://fk/a"},
                                                         {"@type": "ListItem", "name": "A", "url": "https://fk/a"}]})
    page += '<script type="application/ld+json">{not json</script>'
    blocks = jsonld_blocks(page)
    assert item_list_links(blocks) == [("A", "https://fk/a")] and listings_from_jsonld(blocks, "x") == []


def test_shopify_json():
    suggest = {"resources": {"results": {"products": [{"title": "Word Power", "url": "/products/wp?_pos=1"}]}}}
    assert shopify_suggest_handles(suggest) == [("Word Power", "/products/wp")]
    product = {"title": "Word Power Made Easy", "price": 12900, "compare_at_price": 19900, "available": False,
               "tags": ["Norman Lewis"], "description": "<p>Penguin</p>"}
    found = shopify_listing(product, "https://ob/products/wp", "INR")
    assert (found.price, found.list_price, found.available) == (129.0, 199.0, False) and "Norman Lewis" in found.author_text


# ---- Matching ----

def test_author_found_in_description_confirms_the_book():
    book = ident("Word Power Made Easy", "Norman Lewis")
    found = listing("Word Power Made Easy - The Complete Handbook", author_text="Penguin, Norman Lewis")
    assert match_listing(book, found)[0] == 100


def test_disagreeing_author_is_rejected():
    assert match_listing(ident("Mathematics for Class 11", "R.D. Sharma"),
                         listing("Mathematics for Class 11", authors=["R.S. Aggarwal"])) is None


def test_generic_title_without_author_never_matches_a_longer_title():
    assert match_listing(ident("PHYSICS"), listing("Physics: Concepts and Connections")) is None
    assert match_listing(ident("SOCIAL SCIENCE"), listing("Social Science")) is None   # too generic, even exact


def test_specific_title_without_author_matches_and_is_marked():
    score, title_only = match_listing(ident("Sanjiv English Course Class 10", spine="SANJIV ENGLISH COURSE / Class 10"),
                                      listing("Sanjiv English Course Class 10 (2025 edition)"))
    assert title_only and score >= 92


def test_class_from_the_spine_must_agree():
    book = ident("Fundamental Physics", "K.L. Gomber", spine="Pradeep's / FUNDAMENTAL PHYSICS / CLASS XII", publisher="Pradeep's")
    assert match_listing(book, listing("Pradeep's Fundamental Physics for Class 12", author_text="K.L. Gomber")) is not None
    assert match_listing(book, listing("Pradeep's Fundamental Physics for Class 11", author_text="K.L. Gomber")) is None


def test_title_variants_drop_subtitles_and_edition_notes():
    assert "Word Power Made Easy" in title_variants("Word Power Made Easy - The Complete Handbook (Penguin)")


# ---- Arithmetic ----

def quote(price, condition="new", converted=False) -> Quote:
    return Quote(shop="s", url="u", title="t", condition=condition, price=price, currency="INR", price_local=price,
                 converted=converted, match_score=100, retrieved_at=NOW, snapshot_ref="evidence/prices/x.html")


def test_used_value_is_capped_and_needs_a_new_price():
    assert book_values([100, 200, 300], [50, 70]) == (200, 60)
    assert book_values([100], [500]) == (100, 100)
    assert book_values([], [500]) == (None, None)
    assert book_values([], []) == (None, None)


def test_local_prices_are_preferred_over_converted_ones():
    kept = prefer_local([quote(100), quote(95, converted=True), quote(600, "used", converted=True)])
    assert [(q.price, q.condition) for q in kept] == [(100, "new"), (600, "used")]


def test_item_range_and_query():
    assert price_range([10, 20, 30, 40, 50]) == (20, 40) and price_range([7, 3]) == (3, 7) and price_range([]) is None
    box = BBox(x0=0, y0=0, x1=1, y1=1)
    almirah = InventoryItem(item_id="I1", category="shelving", label="cabinet", material="wood", brand=None, unit="A",
                            frame_ref="f", bbox=box, plane_box=box, views=[])
    assert item_query(almirah, {"shelving": "almirah"}) == "wood almirah"
    assert item_matches("wood almirah", "Engineered Wood 2 Door Almirah") and not item_matches("wood almirah", "Steel Bucket")


# ---- Shops and policy ----

class FakeShop:
    """Stands in for ConfiguredShop: fixed listings, or a refusal."""

    def __init__(self, name, listings=(), used_source=False, blocked=False):
        self.name, self.listings, self.blocked_flag, self.searches = name, list(listings), blocked, 0
        self.cfg = ShopConfig(name=name, kinds=["book"], search_url="x", strategy="jsonld", used_source=used_source)

    def search(self, query, relevant):
        self.searches += 1
        if self.blocked_flag:
            raise ShopBlocked(f"{self.name}: HTTP 503")
        return [Found(listing=l, shop=self.name, snapshot_ref="evidence/prices/x.html", retrieved_at=NOW) for l in self.listings]


def pricer(ctx, shops, threshold=5000.0) -> Pricer:
    p = Pricer(ctx, LocaleShops(country="IN", currency="INR", appraisal_threshold=threshold))
    p.shops = shops
    return p


def word_power(price, condition="new", currency="INR") -> Listing:
    return listing("Word Power Made Easy", price, currency, condition, authors=["Norman Lewis"])


def test_stops_after_two_shops_but_still_asks_the_used_source(ctx: RunContext):
    a, b, c = FakeShop("a", [word_power(129)]), FakeShop("b", [word_power(215)]), FakeShop("c", [word_power(300)])
    used = FakeShop("abe", [word_power(90, "used")], used_source=True)
    result = pricer(ctx, [a, b, c, used]).price_book(ident("Word Power Made Easy", "Norman Lewis"))
    assert c.searches == 0 and used.searches == 1
    assert (result.status, result.replacement_cost, result.used_value) == ("priced", 172.0, 90.0)
    assert all(q.snapshot_ref and q.url for q in result.quotes)


def test_appraisal_threshold_sets_no_price(ctx: RunContext):
    result = pricer(ctx, [FakeShop("a", [word_power(9000)])]).price_book(ident("Word Power Made Easy", "Norman Lewis"))
    assert result.status == "needs_appraisal" and result.replacement_cost is None and result.quotes


def test_conversion_is_marked(ctx: RunContext, monkeypatch):
    from claimagent.pricing import fx
    from claimagent.stages import price as price_module

    monkeypatch.setattr(price_module, "rate", lambda ctx, s, t: fx.Rate(95.0, "2026-10-15", "test"))
    abroad = FakeShop("abe", [word_power(2.0, currency="USD")])
    result = pricer(ctx, [abroad]).price_book(ident("Word Power Made Easy", "Norman Lewis"))
    assert result.replacement_cost == 190.0 and result.quotes[0].converted and result.quotes[0].fx_rate == 95.0
    assert any("converted" in r for r in result.review)


def test_blocked_shop_is_reported_and_unreadable_books_are_not_priced(ctx: RunContext):
    p = pricer(ctx, [FakeShop("amazon", blocked=True)])
    assert p.price_book(ident("Word Power Made Easy", "Norman Lewis")).status == "no_quotes"
    assert p.blocked == {"amazon": "amazon: HTTP 503"}
    assert p.price_book(ident(None, status="unidentified")).status == "not_priced"


def test_jsonld_shop_follows_product_links(ctx: RunContext, monkeypatch):
    pages = {
        "https://fk/search?q=word+power": ld({"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "name": "Word Power Made Easy", "url": "https://fk/p1"},
            {"@type": "ListItem", "name": "Garden Hose", "url": "https://fk/p2"}]}),
        "https://fk/p1": ld({"@type": "Product", "name": "Word Power Made Easy", "offers": {"price": 182, "priceCurrency": "INR"}}),
    }
    opened = []

    def fake_fetch(ctx, url):
        opened.append(url)
        return Page(url=url, text=pages[url], retrieved_at=NOW, snapshot_ref=f"evidence/prices/{len(opened)}.html")

    monkeypatch.setattr("claimagent.pricing.shop.fetch_page", fake_fetch)
    shop = ConfiguredShop(ctx, ShopConfig(name="fk", search_url="https://fk/search?q={query}", strategy="jsonld"))
    found = shop.search("word power", relevant=lambda name: "Word" in name)
    assert [f.listing.price for f in found] == [182] and "https://fk/p2" not in opened
    assert found[0].snapshot_ref == "evidence/prices/2.html"   # the product page, not the search page


def test_fetch_page_caches_snapshots_and_reports_refusals(ctx: RunContext, monkeypatch):
    calls = []

    def ok(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, text="<html>price</html>")

    monkeypatch.setattr(fetch_module.httpx, "get", ok)
    first = fetch_page(ctx, "https://shop.example/p")
    second = fetch_page(ctx, "https://shop.example/p")
    assert len(calls) == 1 and first.snapshot_ref == second.snapshot_ref
    assert (ctx.sweep.root / first.snapshot_ref).read_text() == "<html>price</html>"

    monkeypatch.setattr(fetch_module.httpx, "get", lambda url, **kwargs: httpx.Response(503, text="no"))
    with pytest.raises(ShopBlocked, match="HTTP 503"):
        fetch_page(ctx, "https://amazon.example/s")


def test_art_is_priced_only_after_the_user_says_it_is_a_print(ctx: RunContext):
    box = BBox(x0=0, y0=0, x1=1, y1=1)
    art = InventoryItem(item_id="I2", category="art", label="framed painting", material=None, brand=None, unit=None,
                        frame_ref="f", bbox=box, plane_box=box, views=[])
    p = pricer(ctx, [])
    assert p.price_item(art).status == "needs_appraisal"
    assert p.price_item(art, is_print=True).status == "no_quotes"          # priced like any item (no shop here)
    said = [UserFact(kind="print", target=None, text="the painting is only a print")]
    assert print_items([art], said) == {"I2"}
    other = art.model_copy(update={"item_id": "I3"})
    assert print_items([art, other], said) == set()                        # two pieces of art: which one is unclear
    assert print_items([art, other], [UserFact(kind="print", target="i3", text="that one is a print")]) == {"I3"}
