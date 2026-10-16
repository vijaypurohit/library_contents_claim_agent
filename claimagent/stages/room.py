"""Stage 8: Room. Length, width, height, floor area, wall area and shelved wall area, where measurable.

  1. Walls, doors and windows come from detect. A box is measured only in a keyframe that has a
     scale of its own: the card (scale stage) or a known-size object found in the same frame. Box
     sizes are rough for surfaces seen at an angle, so the confidence is kept low.
  2. The same wall seen in two overlapping keyframes is counted once (boxes mapped by homography).
  3. Walls are taken in capture order around the room. With four walls, opposite walls (1 and 3,
     2 and 4) are averaged into length and width, and a difference over 10% is flagged. With two
     adjacent walls they give length and width directly, flagged. More than four walls means the
     room is not a rectangle: no length and width, only a note. Fewer than two: nothing.
  4. Wall area is gross (2 x (length + width) x height); doors and windows are listed separately.
  5. Shelved wall area = width x height of every measured shelving unit (from the measure stage).
Unmeasurable values stay empty and are listed in `review`, never estimated.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.domain.records import BBox
from claimagent.scale.ladder import choose
from claimagent.scale.sources import ScaleEstimate, known_object_scale
from claimagent.stages.detect import DetectOutput, FrameDetections
from claimagent.stages.inventory import InventoryOutput
from claimagent.stages.keyframes import KeyframesOutput
from claimagent.stages.measure import MeasureOutput, long_short
from claimagent.stages.scale import ScaleOutput
from claimagent.vision.geometry import estimate_homography, iou, map_box
from claimagent.vision.imaging import read_image

M2_TO_FT2 = 10.7639
MISMATCH = 0.10              # opposite walls differing by more than this are flagged
SAME_WALL_IOU = 0.3          # wall boxes overlapping this much after mapping are one wall
BOX_MEASURE_FACTOR = 0.6     # a surface measured from its box is less certain than the scale itself

Surface = Literal["wall", "door", "window"]


class MeasuredSurface(BaseModel):
    kind: Surface
    frame: str
    width_m: float
    height_m: float
    scale_method: str
    confidence: float


class RoomOutput(BaseModel):
    length_m: float | None = None
    width_m: float | None = None
    height_m: float | None = None
    floor_area_m2: float | None = None
    floor_area_ft2: float | None = None
    wall_area_m2: float | None = None
    wall_area_ft2: float | None = None
    shelved_wall_area_m2: float | None = None
    shelved_wall_area_ft2: float | None = None
    scale_method: str | None = None
    confidence: float | None = None
    shape_note: str | None = None
    walls: list[MeasuredSurface] = Field(default_factory=list)
    openings: list[MeasuredSurface] = Field(default_factory=list)   # doors and windows
    review: list[str] = Field(default_factory=list)


# ---- Room arithmetic (pure) ----

def to_ft2(m2: float | None) -> float | None:
    return None if m2 is None else round(m2 * M2_TO_FT2, 2)


def room_from_walls(walls: list[MeasuredSurface]) -> tuple[float | None, float | None, str | None, list[str]]:
    """(length, width, shape note, review) from walls in capture order."""
    widths = [w.width_m for w in walls]
    if len(widths) > 4:
        return None, None, f"{len(widths)} walls: not a rectangle", ["room is not a rectangle: length and width not computed"]
    if len(widths) == 4:
        review = []
        for a, b, name in ((widths[0], widths[2], "length"), (widths[1], widths[3], "width")):
            if abs(a - b) / max(a, b) > MISMATCH:
                review.append(f"opposite walls for {name} differ: {a:.2f} m and {b:.2f} m")
        return round((widths[0] + widths[2]) / 2, 2), round((widths[1] + widths[3]) / 2, 2), "rectangle", review
    if len(widths) in (2, 3):
        return round(widths[0], 2), round(widths[1], 2), "rectangle assumed", \
            ["only some walls measured: length and width from two adjacent walls"]
    return None, None, None, ["fewer than two walls measured: room size unknown"]


def merge_walls(surfaces: list[tuple[MeasuredSurface, BBox]], images: dict) -> list[MeasuredSurface]:
    """Drop walls already seen in an earlier overlapping keyframe (kept in capture order).
    `images` holds each wall's frame, for the homography between frames."""
    kept: list[tuple[MeasuredSurface, BBox]] = []
    for surface, box in surfaces:
        duplicate = False
        for other, other_box in kept:
            if other.frame == surface.frame:
                continue
            h = estimate_homography(images[surface.frame], images[other.frame])
            if h is not None and iou(map_box(h, box), other_box) >= SAME_WALL_IOU:
                duplicate = True
                break
        if not duplicate:
            kept.append((surface, box))
    return [s for s, _ in kept]


# ---- Stage ----

@register
class RoomStage(Stage):
    name = "room"
    order = 8
    needs = ("keyframes", "scale", "detect", "inventory", "measure")
    output = RoomOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> RoomOutput:
        keyframes: KeyframesOutput = inputs["keyframes"]  # type: ignore[assignment]
        scale: ScaleOutput = inputs["scale"]  # type: ignore[assignment]
        detect: DetectOutput = inputs["detect"]  # type: ignore[assignment]
        inventory: InventoryOutput = inputs["inventory"]  # type: ignore[assignment]
        measure: MeasureOutput = inputs["measure"]  # type: ignore[assignment]
        times = {k.frame: k.t for k in keyframes.keyframes}
        review: list[str] = []

        # Measure every wall, door and window in frames with a scale of their own.
        measured: list[tuple[MeasuredSurface, BBox, FrameDetections]] = []
        unscaled = 0
        for frame in sorted(detect.frames, key=lambda f: times.get(f.frame, 0.0)):
            surfaces = [o for o in frame.objects if o.valid and o.kind in ("wall", "door", "window")]
            if not surfaces:
                continue
            frame_scale = self._frame_scale(scale, frame)
            if frame_scale is None:
                unscaled += len(surfaces)
                continue
            for obj in surfaces:
                measured.append((MeasuredSurface(
                    kind=obj.kind, frame=frame.frame,  # type: ignore[arg-type]
                    width_m=round(obj.bbox.width * frame_scale.cm_per_px / 100, 2),
                    height_m=round(obj.bbox.height * frame_scale.cm_per_px / 100, 2),
                    scale_method=frame_scale.method, confidence=round(frame_scale.confidence * BOX_MEASURE_FACTOR, 3)),
                    obj.bbox, frame))
        if unscaled:
            review.append(f"{unscaled} wall/door/window box(es) seen in frames without a scale: not measured")

        wall_entries = [m for m in measured if m[0].kind == "wall"]
        images = {m[0].frame: read_image(ctx.sweep.frame_path(m[0].frame)) for m in wall_entries}
        walls = merge_walls([(s, b) for s, b, _ in wall_entries], images) if wall_entries else []
        openings = [m[0] for m in measured if m[0].kind != "wall"]

        length, width, shape, shape_review = room_from_walls(walls)
        review += shape_review
        height = round(statistics.median(w.height_m for w in walls), 2) if walls else None
        floor = round(length * width, 2) if length and width else None
        wall_area = round(2 * (length + width) * height, 2) if length and width and height else None

        shelving = {i.item_id for i in inventory.items if i.category == "shelving"}
        units = [m for m in measure.items if m.item_id in shelving]
        sized = [m for m in units if m.w_cm and m.h_cm]
        shelved = round(sum(m.w_cm * m.h_cm for m in sized) / 1e4, 2) if sized else None
        if len(sized) < len(units):
            review.append(f"{len(units) - len(sized)} shelving unit(s) could not be measured: shelved wall area "
                          + ("incomplete" if sized else "unknown"))

        method = walls[0].scale_method if walls else (sized[0].scale_method if sized else None)
        confidence = min((w.confidence for w in walls), default=None) if walls else (min(m.confidence for m in sized) if sized else None)
        for note in review:
            ctx.warn(f"room: {note}")
        return RoomOutput(length_m=length, width_m=width, height_m=height, floor_area_m2=floor, floor_area_ft2=to_ft2(floor),
                          wall_area_m2=wall_area, wall_area_ft2=to_ft2(wall_area), shelved_wall_area_m2=shelved,
                          shelved_wall_area_ft2=to_ft2(shelved), scale_method=method, confidence=confidence,
                          shape_note=shape, walls=walls, openings=openings, review=review)

    @staticmethod
    def _frame_scale(scale: ScaleOutput, frame: FrameDetections) -> ScaleEstimate | None:
        """The frame's own scale: its card record, or known-size objects detected in it."""
        estimates = []
        card = next((f for f in scale.frames if f.frame == frame.frame and f.method == "card" and f.cm_per_px), None)
        if card is not None:
            estimates.append(ScaleEstimate(method="card", cm_per_px=card.cm_per_px, confidence=card.confidence))
        for obj in frame.objects:
            if obj.kind == "reference" and obj.valid and obj.reference_type:
                estimate = known_object_scale(obj.reference_type, *long_short(obj.bbox))
                if estimate:
                    estimates.append(estimate)
        return choose(estimates).chosen
