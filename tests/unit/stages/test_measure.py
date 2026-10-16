"""Measure stage: each scale rung, axis swap, flags, shelf run, and empty output without a scale."""

import pytest

from claimagent.domain.records import BBox
from claimagent.scale.sources import known_object_scale
from claimagent.stages.detect import DetectedObject, DetectOutput, FrameDetections
from claimagent.stages.identify import IdentifiedBook, IdentifyOutput
from claimagent.stages.inventory import InventoryBook, InventoryItem, InventoryOutput, View
from claimagent.stages.measure import MeasureStage, ShelfScale, catalogue_scale, measure_book, run_length_px
from claimagent.stages.scale import FrameScale, ScaleOutput

REF = "f000000_t000.00.jpg"


def box(x0, y0, x1, y1) -> BBox:
    return BBox(x0=x0, y0=y0, x1=x1, y1=y1)


def inv_book(book_id, b: BBox, shelf=1, orientation="upright", truncated=False) -> InventoryBook:
    return InventoryBook(book_id=book_id, unit="A", shelf=shelf, position=int(book_id[-1]), orientation=orientation,
                         frame_ref=REF, bbox=b, plane_box=b, views=[View(frame=REF, bbox=b, truncated=truncated)],
                         annotated_ref=None, review=[])


def ident(book_id, height=None) -> IdentifiedBook:
    return IdentifiedBook(book_id=book_id, frame_ref=REF, crop_ref="x", spine_text="t", legible=True,
                          status="identified" if height else "spine_only", catalogue_height_cm=height)


def run(books, identified, references=(), card=None, items=()):
    """Run the stage directly on hand-made inputs (it reads no files)."""
    detect = DetectOutput(frames=[FrameDetections(frame=REF, width=4000, height=3000, detector="fake",
                                                  objects=list(references))], failed_frames=[])
    scale = ScaleOutput(frames=[card or FrameScale(frame=REF, method="none")], card_frames=1 if card else 0)
    inventory = InventoryOutput(reference_frame=REF, books=books, items=list(items), shelves=[], unconfirmed=[], unlinked_frames=[])

    class Ctx:
        warnings: list[str] = []

        def warn(self, message):
            self.warnings.append(message)

    ctx = Ctx()
    out = MeasureStage().run(ctx, {"scale": scale, "detect": detect, "inventory": inventory,
                                   "identify": IdentifyOutput(reader="fake", books=identified)})
    return out, ctx.warnings


# ---- Rungs ----

def test_two_catalogue_heights_reproduce_a_third_books_height():
    # 0.025 cm/px: 24 cm = 960 px, 20 cm = 800 px; the third book is 880 px tall, so 22 cm.
    books = [inv_book("A1-1", box(0, 100, 60, 1060)), inv_book("A1-2", box(70, 260, 130, 1060)),
             inv_book("A1-3", box(140, 180, 200, 1060))]
    out, _ = run(books, [ident("A1-1", 24.0), ident("A1-2", 20.0), ident("A1-3")])
    third = out.books[2]
    assert third.height_cm == pytest.approx(22.0, abs=0.1) and third.scale_method == "catalogue_height"
    assert out.shelves[0].confidence > 0.8


def test_median_ignores_one_wrong_catalogue_height():
    estimate = catalogue_scale([(24.0, 960), (20.0, 800), (22.0, 880), (40.0, 900)])  # last height is wrong
    assert estimate.cm_per_px == pytest.approx(0.025)
    assert "1 disagreeing ignored" in estimate.detail


def test_single_catalogue_book_gives_lower_confidence():
    assert catalogue_scale([(24.0, 960)]).confidence == pytest.approx(0.6)


def test_other_shelf_of_the_unit_is_used_when_the_shelf_has_none():
    books = [inv_book("A1-1", box(0, 100, 60, 1060)), inv_book("A2-1", box(0, 1500, 60, 2300), shelf=2)]
    out, _ = run(books, [ident("A1-1", 24.0), ident("A2-1")])
    assert out.books[1].height_cm == pytest.approx(20.0, abs=0.1)
    assert out.shelves[1].confidence < out.shelves[0].confidence


def test_known_object_rung():
    a4 = DetectedObject(id="0-9", kind="reference", label="paper", bbox=box(1000, 0, 1210, 297), reference_type="a4_paper")
    out, _ = run([inv_book("A1-1", box(0, 100, 60, 340))], [ident("A1-1")], references=[a4])
    assert out.books[0].scale_method == "known_object"
    assert out.books[0].height_cm == pytest.approx(24.0, abs=0.2)   # 240 px at 0.1 cm/px
    assert known_object_scale("power_socket", 100, 80) is None       # varies by country: not used


def test_card_rung_outranks_catalogue_and_cross_checks_it():
    h = [[0.025, 0, 0], [0, 0.025, 0], [0, 0, 1]]  # 0.025 cm per pixel everywhere
    card = FrameScale(frame=REF, method="card", cm_per_px=0.025, confidence=0.95, image_to_plane=h)
    out, _ = run([inv_book("A1-1", box(0, 100, 60, 1060))], [ident("A1-1", 24.0)], card=card)
    assert out.shelves[0].method == "card" and out.shelves[0].agreed_with == ["catalogue_height"]


# ---- Book dimensions ----

def test_axes_swap_for_flat_books_and_odd_heights_are_flagged():
    scale = ShelfScale(unit="A", shelf=1, method="card", cm_per_px=0.025, confidence=0.9)
    flat = measure_book(inv_book("A1-5", box(0, 0, 960, 120), orientation="flat"), scale)
    assert (flat.height_cm, flat.thickness_cm) == (24.0, 3.0)
    tiny = measure_book(inv_book("A1-6", box(0, 0, 40, 200)), scale)
    assert tiny.height_cm == 5.0 and "outside 10-45 cm" in tiny.flags[0]   # flagged, not corrected


def test_no_rung_means_no_dimensions_and_a_warning():
    out, warnings = run([inv_book("A1-1", box(0, 100, 60, 1060))], [ident("A1-1")])
    assert out.books[0].height_cm is None and out.books[0].scale_method == "none"
    assert out.shelves[0].method == "none"
    assert any("no scale for shelf A1" in w for w in warnings)


def test_shelf_run_counts_a_flat_stack_once():
    assert run_length_px([box(0, 0, 60, 900), box(70, 0, 130, 900), box(140, 700, 900, 800), box(150, 800, 890, 900)]) == 880


def test_item_depth_is_always_empty_and_cut_off_items_are_not_measured():
    shelving = InventoryItem(item_id="I1", category="shelving", label="almirah", material=None, brand=None, unit="A",
                             frame_ref=REF, bbox=box(500, 50, 3500, 2900), plane_box=box(500, 50, 3500, 2900), views=[])
    cut = shelving.model_copy(update={"item_id": "I2", "plane_box": box(0, 0, 4000, 3000)})
    out, _ = run([inv_book("A1-1", box(600, 100, 660, 1060))], [ident("A1-1", 24.0)], items=[shelving, cut])
    whole, partial = out.items
    assert whole.d_cm is None and whole.w_cm == pytest.approx(75.0) and whole.scale_method == "catalogue_height"
    assert partial.w_cm is None and "cut off" in partial.flags[0]
