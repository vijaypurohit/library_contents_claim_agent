"""Room stage: area arithmetic, wall pairing and shape rules, and empty output without a scale."""

import pytest

from claimagent.core.context import RunContext
from claimagent.domain.records import BBox
from claimagent.stages.detect import DetectedObject, DetectOutput, FrameDetections
from claimagent.stages.inventory import InventoryItem, InventoryOutput
from claimagent.stages.keyframes import Keyframe, KeyframesOutput
from claimagent.stages.measure import MeasuredItem, MeasureOutput
from claimagent.stages.room import MeasuredSurface, RoomStage, room_from_walls, to_ft2
from claimagent.stages.scale import FrameScale, ScaleOutput


def wall(width, height=2.8) -> MeasuredSurface:
    return MeasuredSurface(kind="wall", frame="f", width_m=width, height_m=height, scale_method="card", confidence=0.5)


def test_square_metres_to_square_feet():
    assert to_ft2(10.0) == 107.64 and to_ft2(None) is None


def test_four_walls_average_opposite_sides_and_flag_mismatch():
    length, width, shape, review = room_from_walls([wall(4.0), wall(3.0), wall(4.1), wall(3.6)])
    assert (length, width, shape) == (4.05, 3.3, "rectangle")
    assert review == ["opposite walls for width differ: 3.00 m and 3.60 m"]


def test_two_walls_give_a_flagged_estimate_and_five_are_not_a_rectangle():
    assert room_from_walls([wall(4.0), wall(3.0)])[:3] == (4.0, 3.0, "rectangle assumed")
    length, width, shape, review = room_from_walls([wall(1)] * 5)
    assert (length, width) == (None, None) and "not a rectangle" in shape and review


def test_no_walls_means_no_room_size():
    assert room_from_walls([])[:2] == (None, None)


def run(ctx: RunContext, objects, card: FrameScale | None, shelving: MeasuredItem | None):
    frame = ctx.sweep.frames()[0].name
    keyframes = KeyframesOutput(keyframes=[Keyframe(frame=frame, t=0.0, sharpness=1, view=0, reason="photo")], frames=[])
    scale = ScaleOutput(frames=[card or FrameScale(frame=frame, method="none")], card_frames=1 if card else 0)
    detect = DetectOutput(frames=[FrameDetections(frame=frame, width=64, height=48, detector="fake", objects=objects)],
                          failed_frames=[])
    box = BBox(x0=0, y0=0, x1=10, y1=10)
    items = [InventoryItem(item_id="I1", category="shelving", label="almirah", material=None, brand=None, unit="A",
                           frame_ref=frame, bbox=box, plane_box=box, views=[])]
    measure = MeasureOutput(shelves=[], books=[], items=[shelving or MeasuredItem(item_id="I1", scale_method="none")])
    inventory = InventoryOutput(reference_frame=frame, books=[], items=items, shelves=[], unconfirmed=[], unlinked_frames=[])
    return RoomStage().run(ctx, {"keyframes": keyframes, "scale": scale, "detect": detect, "inventory": inventory,
                                 "measure": measure})


def surface(kind, b) -> DetectedObject:
    return DetectedObject(id=f"0-{kind}", kind=kind, label=kind, bbox=b)


def test_without_any_scale_everything_is_empty_with_review_entries(ctx: RunContext):
    out = run(ctx, [surface("wall", BBox(x0=0, y0=0, x1=60, y1=40))], card=None, shelving=None)
    assert out.length_m is None and out.floor_area_m2 is None and out.shelved_wall_area_m2 is None
    assert any("without a scale" in r for r in out.review) and any("shelving" in r for r in out.review)


def test_card_frame_measures_walls_openings_and_shelved_area(ctx: RunContext):
    frame = ctx.sweep.frames()[0].name
    card = FrameScale(frame=frame, method="card", cm_per_px=5.0, confidence=0.9)   # 5 cm per pixel
    objects = [surface("wall", BBox(x0=0, y0=0, x1=60, y1=48)), surface("door", BBox(x0=10, y0=6, x1=28, y1=46))]
    shelving = MeasuredItem(item_id="I1", w_cm=90, h_cm=180, scale_method="card", confidence=0.9)
    out = run(ctx, objects, card, shelving)
    assert out.walls[0].width_m == pytest.approx(3.0) and out.walls[0].height_m == pytest.approx(2.4)
    assert out.openings[0].kind == "door" and out.openings[0].height_m == pytest.approx(2.0)
    assert out.shelved_wall_area_m2 == pytest.approx(1.62) and out.shelved_wall_area_ft2 == pytest.approx(17.44)
    assert out.length_m is None   # one wall is not enough for a room size
