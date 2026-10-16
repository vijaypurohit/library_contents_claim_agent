"""Stage 6: Measure. Turn every book's box into centimetres, with the method and a confidence.

All books of one inventory share the reference frame's pixel coordinates (`plane_box`), so each
shelf gets one centimetres-per-pixel value, chosen by the scale ladder (scale/ladder.py) from:
  1. card            the printed card, if the scale stage found or carried it into the reference frame
  2. catalogue_height identified books on the same shelf whose printed height the catalogue knows:
                     height_cm / box length for each, median over the books, spread sets the confidence.
                     Books on other shelves of the same unit are used too, with lower confidence.
  3. known_object    detected objects of standard size in the reference frame (A4 paper, card, door)
  4. depth           depth model; not installed in this build, so this rung is never available
Then, per book: height = the box's long side, thickness = its short side (so the axes swap by
themselves for flat books). Heights outside 10-45 cm are flagged, never corrected. A shelf with no
rung gets no dimensions, and every book on it goes to review. No dimension is filled without a method.

Also: shelf run (the shelf length the books occupy) and item width and height. Item depth is
always empty: it can't be seen from the front.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping

import numpy as np
from pydantic import BaseModel, Field

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.domain.records import BBox
from claimagent.scale.ladder import choose
from claimagent.scale.sources import ScaleEstimate, known_object_scale
from claimagent.stages.detect import DetectOutput
from claimagent.stages.identify import IdentifyOutput
from claimagent.stages.inventory import InventoryBook, InventoryOutput, is_truncated
from claimagent.stages.scale import ScaleOutput
from claimagent.vision.geometry import local_scale

HEIGHT_RANGE_CM = (10.0, 45.0)   # outside this a "book height" is probably a box or scale error
CATALOGUE_TRUST = 0.85           # best confidence of a catalogue-height scale (editions vary a little)
SINGLE_BOOK_TRUST = 0.6          # one book can't be cross-checked
OTHER_SHELF_FACTOR = 0.8         # catalogue books from another shelf of the same unit
INLIER_SPREAD = 0.15             # catalogue ratios this far from the median are ignored as wrong heights


class ShelfScale(BaseModel):
    unit: str
    shelf: int
    method: str                       # a ladder rung, or "none"
    cm_per_px: float | None = None
    confidence: float = 0.0
    detail: str = ""
    agreed_with: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    run_cm: float | None = None       # shelf length occupied by books


class MeasuredBook(BaseModel):
    book_id: str
    height_cm: float | None = None
    thickness_cm: float | None = None
    scale_method: str                 # "none" when no rung applied
    confidence: float = 0.0
    flags: list[str] = Field(default_factory=list)


class MeasuredItem(BaseModel):
    item_id: str
    w_cm: float | None = None
    h_cm: float | None = None
    d_cm: None = None                 # never measured from a front view
    scale_method: str
    confidence: float = 0.0
    flags: list[str] = Field(default_factory=list)


class MeasureOutput(BaseModel):
    shelves: list[ShelfScale]
    books: list[MeasuredBook]
    items: list[MeasuredItem]


# ---- Rungs (pure) ----

def long_short(box: BBox) -> tuple[float, float]:
    return max(box.width, box.height), min(box.width, box.height)


def catalogue_scale(pairs: list[tuple[float, float]], trust: float = 1.0) -> ScaleEstimate | None:
    """Scale from (catalogue height cm, box long side px) pairs of identified books.

    The median ratio is used. Ratios far from it (a wrong catalogue height, a box cut short) are
    dropped before the spread is measured; the remaining spread sets the confidence."""
    ratios = [cm / px for cm, px in pairs if px > 0]
    if not ratios:
        return None
    median = statistics.median(ratios)
    inliers = [r for r in ratios if abs(r - median) / median <= INLIER_SPREAD]
    if len(inliers) == 1:
        confidence = SINGLE_BOOK_TRUST
    else:
        spread = (max(inliers) - min(inliers)) / median
        confidence = CATALOGUE_TRUST * max(0.3, 1 - 2 * spread)
    dropped = len(ratios) - len(inliers)
    detail = f"median of {len(inliers)} catalogue height(s)" + (f", {dropped} disagreeing ignored" if dropped else "")
    return ScaleEstimate(method="catalogue_height", cm_per_px=statistics.median(inliers),
                         confidence=round(confidence * trust, 3), detail=detail)


def run_length_px(boxes: list[BBox]) -> float:
    """Horizontal length covered by boxes, counting overlaps (a flat stack, a box over a box) once."""
    total, end = 0.0, float("-inf")
    for x0, x1 in sorted((b.x0, b.x1) for b in boxes):
        if x1 > end:
            total += x1 - max(x0, end)
            end = x1
    return total


def measure_book(book: InventoryBook, scale: ShelfScale) -> MeasuredBook:
    if scale.cm_per_px is None:
        return MeasuredBook(book_id=book.book_id, scale_method="none", flags=["no scale for this shelf"])
    long_px, short_px = long_short(book.plane_box)
    height, thickness = long_px * scale.cm_per_px, short_px * scale.cm_per_px
    flags = []
    if not HEIGHT_RANGE_CM[0] <= height <= HEIGHT_RANGE_CM[1]:
        flags.append(f"height {height:.1f} cm is outside {HEIGHT_RANGE_CM[0]:g}-{HEIGHT_RANGE_CM[1]:g} cm: check")
    if book.views and all(v.truncated for v in book.views):
        flags.append("only seen cut off by a frame edge: size is a lower bound")
    return MeasuredBook(book_id=book.book_id, height_cm=round(height, 1), thickness_cm=round(thickness, 1),
                        scale_method=scale.method, confidence=scale.confidence, flags=flags)


# ---- Stage ----

@register
class MeasureStage(Stage):
    name = "measure"
    order = 6
    needs = ("scale", "detect", "inventory", "identify")
    output = MeasureOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> MeasureOutput:
        scale: ScaleOutput = inputs["scale"]  # type: ignore[assignment]
        detect: DetectOutput = inputs["detect"]  # type: ignore[assignment]
        inventory: InventoryOutput = inputs["inventory"]  # type: ignore[assignment]
        identify: IdentifyOutput = inputs["identify"]  # type: ignore[assignment]
        reference = inventory.reference_frame
        if reference is None:
            return MeasureOutput(shelves=[], books=[], items=[])

        heights = {b.book_id: b.catalogue_height_cm for b in identify.books
                   if b.status == "identified" and b.catalogue_height_cm}
        frame_wide = self._frame_wide(scale, detect, reference)

        shelves: list[ShelfScale] = []
        books: list[MeasuredBook] = []
        for unit, shelf in sorted({(b.unit, b.shelf) for b in inventory.books}):
            members = [b for b in inventory.books if (b.unit, b.shelf) == (unit, shelf)]
            estimates = self._card(scale, reference, members) + frame_wide
            same = catalogue_scale([(heights[b.book_id], long_short(b.plane_box)[0]) for b in members if b.book_id in heights])
            other = catalogue_scale([(heights[b.book_id], long_short(b.plane_box)[0]) for b in inventory.books
                                     if b.unit == unit and b.shelf != shelf and b.book_id in heights], OTHER_SHELF_FACTOR)
            estimates += [e for e in (same or other,) if e]
            choice = choose(estimates)
            if choice.chosen is None:
                record = ShelfScale(unit=unit, shelf=shelf, method="none", detail="no card, catalogue height or known object")
            else:
                c = choice.chosen
                record = ShelfScale(unit=unit, shelf=shelf, method=c.method, cm_per_px=c.cm_per_px, confidence=c.confidence,
                                    detail=c.detail, agreed_with=list(choice.agreed_with), conflicts=choice.conflicts,
                                    run_cm=round(run_length_px([b.plane_box for b in members]) * c.cm_per_px, 1))
                for conflict in choice.conflicts:
                    ctx.warn(f"measure: shelf {unit}{shelf}: {conflict}")
            shelves.append(record)
            books += [measure_book(b, record) for b in members]

        unsized = [s for s in shelves if s.method == "none"]
        if unsized:
            ctx.warn(f"measure: no scale for shelf {', '.join(f'{s.unit}{s.shelf}' for s in unsized)}; "
                     "their books have no dimensions (needs a scale card, identified books with catalogue heights, or a known object)")
        items = [self._item(item, shelves, frame_wide, detect, reference) for item in inventory.items]
        return MeasureOutput(shelves=shelves, books=books, items=items)

    @staticmethod
    def _card(scale: ScaleOutput, reference: str, members: list[InventoryBook]) -> list[ScaleEstimate]:
        """The card rung, measured over the shelf's own region of the reference frame."""
        record = next((f for f in scale.frames if f.frame == reference and f.method == "card"), None)
        if record is None or record.image_to_plane is None:
            return []
        x0, y0 = min(b.plane_box.x0 for b in members), min(b.plane_box.y0 for b in members)
        x1, y1 = max(b.plane_box.x1 for b in members), max(b.plane_box.y1 for b in members)
        region = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        return [ScaleEstimate(method="card", cm_per_px=local_scale(np.asarray(record.image_to_plane), region),
                              confidence=record.confidence, detail=record.detail)]

    @staticmethod
    def _frame_wide(scale: ScaleOutput, detect: DetectOutput, reference: str) -> list[ScaleEstimate]:
        """Known objects in the reference frame: one estimate each."""
        frame = next((f for f in detect.frames if f.frame == reference), None)
        if frame is None:
            return []
        estimates = []
        for obj in frame.objects:
            if obj.kind == "reference" and obj.valid and obj.reference_type:
                estimate = known_object_scale(obj.reference_type, *long_short(obj.bbox))
                if estimate:
                    estimates.append(estimate)
        return estimates

    @staticmethod
    def _item(item, shelves: list[ShelfScale], frame_wide: list[ScaleEstimate], detect: DetectOutput, reference: str) -> MeasuredItem:
        """Width and height of an item in the reference frame; shelving uses its own unit's best shelf scale."""
        candidates = [s for s in shelves if s.cm_per_px and (item.unit is None or s.unit == item.unit)]
        best = max(candidates, key=lambda s: s.confidence, default=None)
        fallback = choose(frame_wide).chosen
        if best is None and fallback is None:
            return MeasuredItem(item_id=item.item_id, scale_method="none", flags=["no scale"])
        cm_per_px, method, confidence = ((best.cm_per_px, best.method, best.confidence) if best
                                         else (fallback.cm_per_px, fallback.method, fallback.confidence))
        frame = next((f for f in detect.frames if f.frame == reference), None)
        if frame is not None and is_truncated(item.plane_box, frame.width, frame.height):
            # A box at the frame edge is the visible part only; its size would understate the item.
            return MeasuredItem(item_id=item.item_id, scale_method=method, confidence=confidence,
                                flags=["cut off by the frame edge: not measured"])
        return MeasuredItem(item_id=item.item_id, w_cm=round(item.plane_box.width * cm_per_px, 1),
                            h_cm=round(item.plane_box.height * cm_per_px, 1), scale_method=method, confidence=confidence)
