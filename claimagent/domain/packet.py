"""The claim packet: the final output of a sweep (schema in docs/DESIGN.md, Appendix A).

Three rules are enforced here, by the data model itself:
1. Unknown values are None (empty), never 0. A filled value must have evidence.
2. Every price carries a source, a URL and a retrieval date, or it cannot be created.
3. Totals are computed from the lines by `compute_totals`. A packet whose totals differ from
   the lines (for example, totals written by a language model) fails validation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from claimagent.domain.records import BBox


class _Sourced(BaseModel):
    """Where a price came from. Shared by book and item prices."""

    source: str | None = None
    url: str | None = None
    retrieved_at: datetime | None = None
    snapshot: str | None = None  # path of the saved page, relative to the sweep folder

    def _require_source(self, has_amount: bool) -> None:
        if has_amount and not (self.source and self.url and self.retrieved_at):
            raise ValueError("a price needs a source, a URL and a retrieval date")


class BookPrice(_Sourced):
    """One price for a book, in the sweep's currency."""

    amount: float | None = None
    converted: bool = False  # True when converted from another market's currency

    @model_validator(mode="after")
    def _sourced(self) -> BookPrice:
        self._require_source(self.amount is not None)
        return self


class UsedValue(BookPrice):
    condition_assumed: str | None = None


class ItemPrice(_Sourced):
    """Replacement cost of a non-book item. low == high for an exact price; a range when brand/model is unknown."""

    low: float | None = None
    high: float | None = None

    @model_validator(mode="after")
    def _sourced(self) -> ItemPrice:
        if (self.low is None) != (self.high is None):
            raise ValueError("an item price range needs both low and high")
        if self.low is not None and self.low > self.high:
            raise ValueError("item price low is above high")
        self._require_source(self.low is not None)
        return self


class Dimensions(BaseModel):
    w: float | None = None
    h: float | None = None
    d: float | None = None


class SweepInfo(BaseModel):
    id: str
    captured_at: datetime
    device: str | None = None
    duration_s: float | None = None
    country: str
    currency: str


class Room(BaseModel):
    length_m: float | None = None
    width_m: float | None = None
    height_m: float | None = None
    floor_area_m2: float | None = None
    wall_area_m2: float | None = None
    shelved_wall_area_m2: float | None = None
    scale_method: str | None = None
    confidence: float | None = None


class Book(BaseModel):
    """One physical book, located on a shelf and traced to the frame it was seen in."""

    id: str
    unit: str | None = None       # shelving unit: A, B, ...
    shelf: str | None = None      # shelf row, 1 = top
    position: int | None = None   # order from the left on that shelf
    frame_ref: str                # frame file the book was seen in
    bbox: BBox | None = None
    annotated_ref: str | None = None  # shelf image with this book's numbered box
    status: Literal["identified", "unidentified", "needs_appraisal"]
    title: str | None = None
    author: str | None = None
    edition: str | None = None
    isbn: str | None = None
    spine_height_cm: float | None = None
    spine_thickness_cm: float | None = None
    dimension_confidence: float | None = None
    scale_method: str | None = None   # how centimetres were obtained: card, known_object, depth
    id_confidence: float | None = None
    replacement_cost: BookPrice = Field(default_factory=BookPrice)
    used_value: UsedValue = Field(default_factory=UsedValue)

    @model_validator(mode="after")
    def _no_title_without_identification(self) -> Book:
        if self.status == "unidentified" and self.title:
            raise ValueError(f"book {self.id} is unidentified but has a title")
        return self


class Item(BaseModel):
    """A non-book item (furniture, lamp, art, ...)."""

    id: str
    category: str
    description: str | None = None
    brand_model: str | None = None
    frame_ref: str
    bbox: BBox | None = None
    dimensions_cm: Dimensions = Field(default_factory=Dimensions)
    status: Literal["priced", "range", "needs_appraisal", "unpriced"]  # "unpriced": no sourced price found
    replacement_cost: ItemPrice = Field(default_factory=ItemPrice)
    confidence: float | None = None


class Totals(BaseModel):
    book_count: int
    books_identified: int
    books_unidentified: int
    shelf_run_m: float
    books_replacement_cost: float
    books_used_value: float
    items_replacement_cost_low: float
    items_replacement_cost_high: float
    excluded_from_totals: int


class ReviewEntry(BaseModel):
    """A line a person should check, and why."""

    ref_id: str
    reason: str


# A line counts towards the money totals only if it is priced; everything else is "excluded".
def _book_priced(book: Book) -> bool:
    return book.status == "identified" and book.replacement_cost.amount is not None


def _item_priced(item: Item) -> bool:
    return item.status in ("priced", "range") and item.replacement_cost.low is not None


def compute_totals(books: Sequence[Book], items: Sequence[Item]) -> Totals:
    """The only place totals are calculated. Unpriced and appraisal-bound lines are counted as excluded."""
    priced_books = [b for b in books if _book_priced(b)]
    priced_items = [i for i in items if _item_priced(i)]
    return Totals(
        book_count=len(books),
        books_identified=sum(b.status == "identified" for b in books),
        books_unidentified=sum(b.status == "unidentified" for b in books),
        shelf_run_m=round(sum(b.spine_thickness_cm or 0.0 for b in books) / 100, 3),
        books_replacement_cost=round(sum(b.replacement_cost.amount for b in priced_books), 2),
        books_used_value=round(sum(b.used_value.amount or 0.0 for b in priced_books), 2),
        items_replacement_cost_low=round(sum(i.replacement_cost.low for i in priced_items), 2),
        items_replacement_cost_high=round(sum(i.replacement_cost.high for i in priced_items), 2),
        excluded_from_totals=(len(books) - len(priced_books)) + (len(items) - len(priced_items)),
    )


class ClaimPacket(BaseModel):
    sweep: SweepInfo
    room: Room = Field(default_factory=Room)
    books: list[Book] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    totals: Totals
    review_queue: list[ReviewEntry] = Field(default_factory=list)

    @classmethod
    def build(cls, sweep: SweepInfo, room: Room, books: list[Book], items: list[Item],
              review_queue: list[ReviewEntry]) -> ClaimPacket:
        """Create a packet; totals are always derived from the lines."""
        return cls(sweep=sweep, room=room, books=books, items=items,
                   totals=compute_totals(books, items), review_queue=review_queue)

    @model_validator(mode="after")
    def _totals_match_lines(self) -> ClaimPacket:
        expected = compute_totals(self.books, self.items)
        if self.totals != expected:
            raise ValueError(f"totals do not match the lines: expected {expected.model_dump()}")
        return self
