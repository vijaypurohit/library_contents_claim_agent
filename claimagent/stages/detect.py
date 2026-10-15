"""Stage 3: Detect. Box every book spine, non-book item, scale reference, wall, door and window.

For each keyframe the "detect" model provider returns boxes; code then checks them:
- the box must lie inside the image and have a sensible size
- a spine must be clearly longer than it is wide (in either direction); its orientation
  (upright or flat) is taken from the box shape, not from the model
If most spines in an answer fail these checks, the answer is rejected and the next provider is tried.
Rejected boxes are kept in the output with the reason, so a person can see what was discarded.

Output: per keyframe, the detected objects in pixel coordinates; plus debug/det_<frame>.jpg overlays.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.domain.records import BBox
from claimagent.prompts import load_prompt
from claimagent.providers.base import ProviderUnavailable
from claimagent.stages.keyframes import KeyframesOutput
from claimagent.vision.imaging import draw_boxes, encode_jpeg, read_image, write_jpeg

Kind = Literal["spine", "item", "reference", "wall", "door", "window"]
ITEM_CATEGORIES = {"shelving", "furniture", "appliance", "lighting", "art", "rug", "electronics", "decor", "other"}

SEND_MAX_SIDE = 2048       # image size sent to the model; boxes come back normalised, so this is enough
MIN_SIDE_PX = 4            # boxes thinner than this are noise
MIN_AREA_SHARE = 0.0002    # 0.02% of the frame: a distant thin spine in 4K is still above this
MAX_SPINE_AREA_SHARE = 0.25
MIN_SPINE_ELONGATION = 1.3 # long side / short side; thick books are stubby, but never square
MAX_INVALID_SPINE_SHARE = 0.5
PARALLEL_CALLS = 4

# Overlay colours (BGR) for the debug images.
COLOURS = {"spine": (60, 200, 60), "item": (255, 140, 0), "reference": (255, 0, 255),
           "wall": (0, 220, 220), "door": (0, 220, 220), "window": (0, 220, 220), "invalid": (0, 0, 255)}


# ---- What the model is asked to return (validated by the gateway) ----

class ModelObject(BaseModel):
    kind: Kind
    label: str
    box_2d: list[float] = Field(min_length=4, max_length=4)  # [ymin, xmin, ymax, xmax], 0-1000
    category: str | None = None
    reference_type: str | None = None
    material: str | None = None
    brand: str | None = None
    uncertain: bool = False


class ModelAnswer(BaseModel):
    objects: list[ModelObject] = Field(default_factory=list)


# ---- What this stage writes ----

class DetectedObject(BaseModel):
    id: str                                   # "<frame index>-<n>", unique within the sweep
    kind: Kind
    label: str
    bbox: BBox                                # pixels of the frame
    orientation: Literal["upright", "flat"] | None = None  # spines only
    category: str | None = None
    reference_type: str | None = None
    material: str | None = None
    brand: str | None = None
    uncertain: bool = False
    valid: bool = True
    reject_reason: str | None = None


class FrameDetections(BaseModel):
    frame: str
    width: int
    height: int
    detector: str | None                      # provider chain used; None if every provider failed
    objects: list[DetectedObject]


class DetectOutput(BaseModel):
    frames: list[FrameDetections]
    failed_frames: list[str]                  # keyframes no provider could analyse: go to review


def to_pixels(box_2d: list[float], width: int, height: int) -> BBox:
    """Convert [ymin, xmin, ymax, xmax] on a 0-1000 scale to a pixel box, clipped to the image."""
    ymin, xmin, ymax, xmax = (min(max(v, 0.0), 1000.0) / 1000.0 for v in box_2d)
    return BBox(x0=xmin * width, y0=ymin * height, x1=xmax * width, y1=ymax * height)


def check_box(kind: str, box: BBox, width: int, height: int) -> str | None:
    """Reason a box is implausible, or None if it passes."""
    if box.width < MIN_SIDE_PX or box.height < MIN_SIDE_PX:
        return "box too thin"
    share = box.area / (width * height)
    if share < MIN_AREA_SHARE:
        return "box too small"
    if kind == "spine":
        if share > MAX_SPINE_AREA_SHARE:
            return "spine box covers too much of the frame"
        if max(box.width, box.height) / min(box.width, box.height) < MIN_SPINE_ELONGATION:
            return "spine box is almost square"
    return None


def interpret(answer: ModelAnswer, frame_index: int, width: int, height: int) -> list[DetectedObject]:
    """Turn a model answer into checked pixel-space objects."""
    objects = []
    for n, raw in enumerate(answer.objects, start=1):
        box = to_pixels(raw.box_2d, width, height)
        reason = check_box(raw.kind, box, width, height)
        category = None
        if raw.kind == "item":
            category = (raw.category or "other").lower()
            category = category if category in ITEM_CATEGORIES else "other"
        objects.append(DetectedObject(
            id=f"{frame_index}-{n}",
            kind=raw.kind,
            label=raw.label,
            bbox=box,
            # Orientation comes from geometry: a box taller than wide is an upright spine.
            orientation=("upright" if box.height >= box.width else "flat") if raw.kind == "spine" else None,
            category=category,
            reference_type=raw.reference_type if raw.kind == "reference" else None,
            material=raw.material,
            brand=raw.brand,
            uncertain=raw.uncertain,
            valid=reason is None,
            reject_reason=reason,
        ))
    return objects


def _answer_check(width: int, height: int):
    """Acceptance test for a whole answer: reject it when most of its spine boxes are implausible."""
    def accept(answer: ModelAnswer) -> str | None:
        spines = [o for o in answer.objects if o.kind == "spine"]
        if not spines:
            return None
        invalid = sum(check_box("spine", to_pixels(o.box_2d, width, height), width, height) is not None for o in spines)
        if invalid / len(spines) > MAX_INVALID_SPINE_SHARE:
            return f"{invalid} of {len(spines)} spine boxes are implausible"
        return None
    return accept


@register
class DetectStage(Stage):
    name = "detect"
    order = 3
    needs = ("keyframes",)
    output = DetectOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> DetectOutput:
        keyframes: KeyframesOutput = inputs["keyframes"]  # type: ignore[assignment]
        model = ctx.vision("detect")
        prompt = load_prompt("detect")

        def detect_one(frame: str) -> FrameDetections:
            image = read_image(ctx.sweep.frame_path(frame))
            height, width = image.shape[:2]
            index = int(frame[1:7])
            try:
                answer = model.generate_json(prompt, [encode_jpeg(image, 85, SEND_MAX_SIDE)], ModelAnswer,
                                             accept=_answer_check(width, height))
            except ProviderUnavailable as exc:
                ctx.warn(f"detect: no provider could analyse {frame} ({exc})")
                return FrameDetections(frame=frame, width=width, height=height, detector=None, objects=[])
            objects = interpret(answer, index, width, height)
            _write_overlay(ctx, frame, image, objects)
            return FrameDetections(frame=frame, width=width, height=height, detector=model.name, objects=objects)

        # Frames are independent, so several model calls run at once.
        with ThreadPoolExecutor(PARALLEL_CALLS) as pool:
            results = list(pool.map(detect_one, [k.frame for k in keyframes.keyframes]))
        return DetectOutput(frames=results, failed_frames=[r.frame for r in results if r.detector is None])


def _write_overlay(ctx: RunContext, frame: str, image, objects: list[DetectedObject]) -> None:
    """debug/det_<frame>.jpg: spines numbered in green, items orange, references magenta, rejected red."""
    out = image
    for kind in ("wall", "door", "window", "item", "reference", "spine"):
        chosen = [o for o in objects if o.kind == kind and o.valid]
        if chosen:
            labels = [str(i + 1) for i in range(len(chosen))] if kind == "spine" else [o.category or o.reference_type or o.kind for o in chosen]
            out = draw_boxes(out, [o.bbox for o in chosen], labels, COLOURS[kind])
    rejected = [o for o in objects if not o.valid]
    if rejected:
        out = draw_boxes(out, [o.bbox for o in rejected], ["x"] * len(rejected), COLOURS["invalid"])
    write_jpeg(ctx.sweep.debug_dir / f"det_{frame}", out)
