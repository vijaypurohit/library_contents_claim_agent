"""Inventory stage: merging spines across views, confirming books, and numbering shelves."""

from datetime import UTC, datetime

import cv2
import numpy as np

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.stage import Stage
from claimagent.core.sweep import frame_name
from claimagent.domain.records import BBox, FrameInfo, Manifest
from claimagent.stages.detect import DetectedObject, DetectOutput, FrameDetections
from claimagent.stages.inventory import (InventoryStage, Observation, assign_units, drop_duplicates,
                                         drop_nested_items, merge, same_orientation, shelf_positions)
from claimagent.stages.keyframes import KeyframesStage
from claimagent.vision.imaging import write_jpeg

FRAME_W, FRAME_H = 1200, 900


def box(x0, y0, x1, y1) -> BBox:
    return BBox(x0=x0, y0=y0, x1=x1, y1=y1)


def spine(b: BBox, n: int = 1, **extra) -> DetectedObject:
    orientation = "upright" if b.height >= b.width else "flat"
    return DetectedObject(id=f"0-{n}", kind="spine", label="book", bbox=b, orientation=orientation, **extra)


def item(b: BBox, n: int = 1, category="shelving") -> DetectedObject:
    return DetectedObject(id=f"0-{n}", kind="item", label=category, bbox=b, category=category)


def scene(width=2400, height=FRAME_H, seed=0) -> np.ndarray:
    """A textured wall, wide enough to be seen as several overlapping frames."""
    rng = np.random.default_rng(seed)
    img = np.zeros((height, width, 3), np.uint8)
    for _ in range(900):
        x, y = rng.integers(0, width - 40), rng.integers(0, height - 40)
        cv2.rectangle(img, (int(x), int(y)), (int(x + rng.integers(10, 40)), int(y + rng.integers(10, 40))),
                      tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    return img


def run_inventory(ctx: RunContext, crops: list[tuple[np.ndarray, int]], books: list[BBox],
                  extra: dict[int, list[DetectedObject]] | None = None):
    """Make one photo per (image, x offset) crop of a wide scene, 'detect' every scene box that lies
    fully inside each crop (in that crop's pixels), and run keyframes + inventory."""
    for path in ctx.sweep.frames_dir.glob("*.jpg"):
        path.unlink()
    infos, detections = [], []
    for i, (img, offset) in enumerate(crops):
        name = frame_name(i, i * 0.25)
        write_jpeg(ctx.sweep.frame_path(name), img, 95)
        infos.append(FrameInfo(name=name, index=i, t=i * 0.25, source=f"{i}.heic", width=img.shape[1], height=img.shape[0]))
        objects = [spine(box(b.x0 - offset, b.y0, b.x1 - offset, b.y1), n)
                   for n, b in enumerate(books, start=1) if offset <= b.x0 and b.x1 <= offset + img.shape[1]]
        objects += (extra or {}).get(i, [])
        detections.append(FrameDetections(frame=name, width=img.shape[1], height=img.shape[0], detector="fake", objects=objects))
    ctx.sweep.write_manifest(Manifest(sweep_id="test", created_at=datetime.now(UTC), sources=["x"], fps=4, frames=infos))

    class FakeDetect(Stage):
        name, order, needs, output = "detect", 3, ("keyframes",), DetectOutput

        def run(self, ctx, inputs):
            return DetectOutput(frames=detections, failed_frames=[])

    runner = Runner(ctx, {"keyframes": KeyframesStage, "detect": FakeDetect, "inventory": InventoryStage})
    runner.run_stage("keyframes")
    runner.run_stage("detect")
    return runner.run_stage("inventory")


# Six upright books on one shelf of a 2400 px wide wall, and four on a second shelf below.
TOP = [box(x, 100, x + 70, 400) for x in (150, 400, 700, 950, 1350, 1600)]
BOTTOM = [box(x, 500, x + 70, 820) for x in (200, 760, 1400, 1650)]


# ---- End to end on synthetic views ----

def test_two_overlapping_views_count_each_book_once(ctx: RunContext):
    wall = scene()
    out = run_inventory(ctx, [(wall[:, 0:FRAME_W], 0), (wall[:, 600:600 + FRAME_W], 600)], TOP + BOTTOM)
    books = out.data["books"]
    assert len(books) == 10
    assert [b["book_id"] for b in books][:6] == ["A1-1", "A1-2", "A1-3", "A1-4", "A1-5", "A1-6"]
    assert [b["book_id"] for b in books][6:] == ["A2-1", "A2-2", "A2-3", "A2-4"]
    shared = [b["book_id"] for b in books if len(b["views"]) == 2]  # books inside both crops: merged, not duplicated
    assert shared == ["A1-3", "A1-4", "A2-2"]
    assert out.data["unconfirmed"] == [] and out.data["unlinked_frames"] == []
    assert (ctx.sweep.evidence_dir / "shelf_A1.jpg").exists() and (ctx.sweep.evidence_dir / "shelf_A2.jpg").exists()


def test_box_seen_once_inside_the_reference_view_is_not_counted(ctx: RunContext):
    wall = scene()
    # Two more books only the second frame sees make it the reference frame (6 spines against 5).
    books = TOP + [box(1480, 100, 1540, 400), box(1720, 100, 1780, 400)]
    # A stray box in the first frame at scene x 1080-1120: inside the reference view, where it shows no spine.
    stray = {0: [spine(box(1080, 120, 1120, 380), 99)]}
    out = run_inventory(ctx, [(wall[:, 0:FRAME_W], 0), (wall[:, 600:600 + FRAME_W], 600)], books, extra=stray)
    assert out.data["reference_frame"] == frame_name(1, 0.25)
    assert len(out.data["books"]) == 8           # books 150 and 400 are outside the reference view, so still counted
    assert [u["frame"] for u in out.data["unconfirmed"]] == [frame_name(0, 0.0)]
    assert any("not visible in the reference frame" in r for b in out.data["books"] for r in b["review"])


def test_unrelated_frame_is_left_out_and_warned(ctx: RunContext):
    wall, other = scene(), scene(seed=9)
    out = run_inventory(ctx, [(wall[:, 0:FRAME_W], 0), (other[:, 0:FRAME_W], 0)], TOP[:3])
    assert len(out.data["books"]) == 3            # the unrelated frame's identical-looking boxes are not added
    assert out.data["unlinked_frames"] == [frame_name(1, 0.25)]
    assert any("could not be linked" in w for w in out.warnings)


# ---- Merging rules ----

def obs(frame, b: BBox, footprint: BBox | None = None, n=1) -> Observation:
    return Observation(frame=frame, obj=spine(b, n), plane_box=b, truncated=False, footprint=footprint)


def test_partial_box_at_a_frame_edge_joins_the_full_book():
    full = obs("ref", box(100, 0, 160, 300), footprint=box(0, 0, 1000, 800))
    # The other frame starts at x=130, so it sees only the right half of the book.
    partial = obs("other", box(130, 0, 160, 300), footprint=box(130, 0, 1130, 800))
    clusters = merge([[full], [partial]], same_orientation, 0.5)
    assert len(clusters) == 1 and clusters[0].frames == {"ref", "other"}


def test_matching_is_one_to_one_per_frame():
    a, b = obs("ref", box(0, 0, 50, 300), n=1), obs("ref", box(60, 0, 110, 300), n=2)
    # Two boxes in the next frame both overlapping book a: only one may join it.
    c, d = obs("next", box(2, 0, 52, 300), n=1), obs("next", box(5, 0, 50, 250), n=2)
    clusters = merge([[a, b], [c, d]], same_orientation, 0.5)
    assert sorted(len(cl.views) for cl in clusters) == [1, 1, 2]


def test_flat_and_upright_boxes_never_merge():
    upright = obs("ref", box(0, 0, 60, 300))
    flat = Observation(frame="next", obj=spine(box(0, 200, 300, 260)), plane_box=box(0, 200, 300, 260), truncated=False)
    assert len(merge([[upright], [flat]], same_orientation, 0.1)) == 2


def test_duplicate_box_in_one_frame_keeps_the_tighter_one():
    loose, tight, other = spine(box(0, 0, 120, 400), 1), spine(box(20, 10, 115, 400), 2), spine(box(300, 0, 360, 400), 3)
    assert [s.id for s in drop_duplicates([loose, tight, other])] == ["0-2", "0-3"]


def test_nested_item_of_the_same_category_is_dropped():
    whole, part, lamp = item(box(0, 0, 1000, 1000), 1), item(box(0, 100, 600, 900), 2), item(box(100, 100, 200, 200), 3, "lighting")
    assert [o.id for o in drop_nested_items([whole, part, lamp])] == ["0-1", "0-3"]


# ---- Layout ----

def test_rows_are_shelves_and_flat_stack_is_numbered_top_first():
    boxes = [box(300, 600, 360, 900),            # bottom shelf, right
             box(100, 600, 160, 900),            # bottom shelf, left
             box(100, 50, 160, 400),             # top shelf, upright
             box(200, 330, 500, 400),            # top shelf, lower flat book of a stack
             box(205, 260, 495, 330)]            # top shelf, upper flat book of the stack
    flat = [False, False, False, True, True]
    assert shelf_positions(boxes, flat) == [(2, 2), (2, 1), (1, 1), (1, 3), (1, 2)]


def test_two_separate_shelving_units_are_lettered_left_to_right():
    books = [box(1200, 100, 1260, 400), box(100, 100, 160, 400)]
    right, left, whole_part = box(1000, 0, 1800, 900), box(0, 0, 800, 900), box(0, 0, 400, 900)
    units, unit_boxes = assign_units(books, [right, left, whole_part])  # whole_part is nested in `left`
    assert units == ["B", "A"] and unit_boxes == [left, right]


def test_one_shelving_box_or_none_means_unit_a():
    books = [box(100, 100, 160, 400)]
    assert assign_units(books, [])[0] == ["A"]
    assert assign_units(books, [box(0, 0, 800, 900)])[0] == ["A"]
