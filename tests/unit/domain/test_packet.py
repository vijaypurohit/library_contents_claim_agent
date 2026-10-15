from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from claimagent.domain.packet import (
    Book,
    BookPrice,
    ClaimPacket,
    Item,
    ItemPrice,
    Room,
    SweepInfo,
    UsedValue,
)

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def price(amount):
    return BookPrice(amount=amount, source="shop", url="https://shop/x", retrieved_at=NOW)


def book(id_, status="identified", amount=None, used=None, thickness=None):
    return Book(
        id=id_, frame_ref="f000001_t000.25.jpg", status=status,
        title="T" if status != "unidentified" else None,
        spine_thickness_cm=thickness,
        replacement_cost=price(amount) if amount is not None else BookPrice(),
        used_value=UsedValue(amount=used, source="shop", url="https://shop/u", retrieved_at=NOW) if used else UsedValue(),
    )


def item(id_, low=None, high=None, status="range"):
    cost = ItemPrice(low=low, high=high, source="s", url="https://s", retrieved_at=NOW) if low is not None else ItemPrice()
    return Item(id=id_, category="lighting", frame_ref="f000002_t000.50.jpg", status=status, replacement_cost=cost)


SWEEP = SweepInfo(id="s", captured_at=NOW, country="IN", currency="INR")


def test_price_without_url_is_rejected():
    with pytest.raises(ValidationError, match="source, a URL and a retrieval date"):
        BookPrice(amount=100, source="shop")


def test_empty_price_is_allowed():
    assert BookPrice().amount is None


def test_unidentified_book_cannot_have_a_title():
    with pytest.raises(ValidationError, match="unidentified but has a title"):
        Book(id="b", frame_ref="f", status="unidentified", title="Guess")


def test_item_range_must_be_complete_and_ordered():
    with pytest.raises(ValidationError):
        ItemPrice(low=10, source="s", url="u", retrieved_at=NOW)
    with pytest.raises(ValidationError, match="low is above high"):
        ItemPrice(low=20, high=10, source="s", url="u", retrieved_at=NOW)


def test_totals_are_computed_from_lines():
    books = [
        book("b1", amount=300, used=120, thickness=2.5),
        book("b2", amount=None, thickness=3.0),          # identified, no price: excluded
        book("b3", status="unidentified", thickness=1.5),  # excluded
        book("b4", status="needs_appraisal"),               # excluded
    ]
    items = [item("i1", 1000, 1500), item("i2", status="needs_appraisal")]
    packet = ClaimPacket.build(SWEEP, Room(), books, items, [])
    t = packet.totals
    assert (t.book_count, t.books_identified, t.books_unidentified) == (4, 2, 1)
    assert t.books_replacement_cost == 300 and t.books_used_value == 120
    assert (t.items_replacement_cost_low, t.items_replacement_cost_high) == (1000, 1500)
    assert t.shelf_run_m == 0.07
    assert t.excluded_from_totals == 4


def test_hand_written_totals_are_rejected():
    packet = ClaimPacket.build(SWEEP, Room(), [book("b1", amount=300)], [], [])
    data = packet.model_dump()
    data["totals"]["books_replacement_cost"] = 999
    with pytest.raises(ValidationError, match="totals do not match"):
        ClaimPacket.model_validate(data)
