"""Stage 2: Scale. Measure centimetres per pixel wherever the printed scale card supports it.

For every keyframe:
  1. look for the scale card (`CardScale`); if found, its corners give an exact image-to-plane mapping
  2. otherwise, try to carry the scale over from the nearest card frame (by time) that shows the
     same scene: a feature homography maps this frame onto the card frame, and chaining it with the
     card frame's mapping gives this frame's mapping. Carried-over scales get a lower confidence.
  3. if neither works, the frame gets `method: none`. No scale is ever invented here.

The other rungs of the scale ladder (catalogue book heights, known objects, depth) are applied in
the measure stage, because they need detections and identifications from later stages.

Output: one record per keyframe, plus debug/scale_<frame>.jpg with the card outlined where found.
"""

from __future__ import annotations

from collections.abc import Mapping

import cv2
import numpy as np
from pydantic import BaseModel

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.scale.sources import CardScale, ScaleEstimate
from claimagent.stages.keyframes import Keyframe, KeyframesOutput
from claimagent.vision.geometry import estimate_homography, local_scale, map_points, overlap_fraction
from claimagent.vision.imaging import read_image, write_jpeg

PROPAGATION_DISCOUNT = 0.8   # a carried-over scale is less certain than one measured in the frame itself
MAX_NEIGHBOURS = 3           # card frames tried per frame, nearest in time first
MIN_OVERLAP = 0.2            # the two frames must share at least this much of the card frame
MAX_SCALE_CHANGE = 4.0       # a chained mapping that changes scale more than this is a bad match


class FrameScale(BaseModel):
    frame: str
    method: str                              # "card", or "none" when nothing in this stage could size the frame
    cm_per_px: float | None = None           # at the card's position in this frame
    confidence: float = 0.0
    plane_id: str | None = None
    image_to_plane: list[list[float]] | None = None  # 3x3 homography: this frame's pixels -> plane cm
    region_px: list[list[float]] | None = None       # card corners in this frame (may lie outside it if carried over)
    propagated_from: str | None = None
    detail: str = ""


class ScaleOutput(BaseModel):
    frames: list[FrameScale]
    card_frames: int                         # keyframes where the card itself was seen


def propagate(image: np.ndarray, card_image: np.ndarray, card: ScaleEstimate, card_frame: str) -> ScaleEstimate | None:
    """Carry a card frame's scale over to `image`, or None if the two frames don't reliably match."""
    to_card = estimate_homography(image, card_image)  # this frame's pixels -> card frame's pixels
    if to_card is None or overlap_fraction(to_card, image.shape, card_image.shape) < MIN_OVERLAP:
        return None
    chained = np.asarray(card.image_to_plane) @ to_card          # this frame's pixels -> plane cm
    region = map_points(np.linalg.inv(to_card), np.asarray(card.region_px))  # where the card would be here
    try:
        cm_per_px = local_scale(chained, region)
    except ValueError:
        return None
    if not 1 / MAX_SCALE_CHANGE <= cm_per_px / card.cm_per_px <= MAX_SCALE_CHANGE:
        return None
    return card.model_copy(update={
        "cm_per_px": cm_per_px,
        "confidence": round(card.confidence * PROPAGATION_DISCOUNT, 3),
        "image_to_plane": chained.tolist(),
        "region_px": region.tolist(),
        "propagated_from": card_frame,
        "detail": f"carried over from {card_frame} by feature homography",
    })


@register
class ScaleStage(Stage):
    name = "scale"
    order = 2
    needs = ("keyframes",)
    output = ScaleOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> ScaleOutput:
        keyframes: KeyframesOutput = inputs["keyframes"]  # type: ignore[assignment]
        card = CardScale(ctx.settings.marker)
        images = {k.frame: read_image(ctx.sweep.frame_path(k.frame)) for k in keyframes.keyframes}

        # Pass 1: frames where the card itself is visible.
        measured: dict[str, ScaleEstimate] = {}
        for k in keyframes.keyframes:
            estimate = card.estimate(images[k.frame])
            if estimate is not None:
                measured[k.frame] = estimate
                _write_overlay(ctx, k.frame, images[k.frame], estimate)

        # Pass 2: every other frame borrows from the nearest card frame that shows the same scene.
        times = {k.frame: k.t for k in keyframes.keyframes}
        results = []
        for k in keyframes.keyframes:
            estimate = measured.get(k.frame) or self._borrow(k, measured, images, times)
            results.append(_record(k.frame, estimate))

        if not measured:
            ctx.warn("scale: no scale card in any keyframe; sizes will come from the other scale rungs in the measure stage")
        unsized = [r.frame for r in results if r.method == "none"]
        if measured and unsized:
            ctx.warn(f"scale: {len(unsized)} keyframe(s) could not be linked to a card frame: {', '.join(unsized)}")
        return ScaleOutput(frames=results, card_frames=len(measured))

    @staticmethod
    def _borrow(k: Keyframe, measured: dict[str, ScaleEstimate], images: dict[str, np.ndarray],
                times: dict[str, float]) -> ScaleEstimate | None:
        nearest = sorted(measured, key=lambda f: abs(times[f] - k.t))[:MAX_NEIGHBOURS]
        for card_frame in nearest:
            estimate = propagate(images[k.frame], images[card_frame], measured[card_frame], card_frame)
            if estimate is not None:
                return estimate
        return None


def _record(frame: str, estimate: ScaleEstimate | None) -> FrameScale:
    if estimate is None:
        return FrameScale(frame=frame, method="none", detail="no scale card in or linked to this frame")
    return FrameScale(frame=frame, method=estimate.method, cm_per_px=estimate.cm_per_px, confidence=estimate.confidence,
                      plane_id=estimate.plane_id, image_to_plane=estimate.image_to_plane, region_px=estimate.region_px,
                      propagated_from=estimate.propagated_from, detail=estimate.detail)


def _write_overlay(ctx: RunContext, frame: str, image: np.ndarray, estimate: ScaleEstimate) -> None:
    """debug/scale_<frame>.jpg: the detected card outlined in magenta, with its cm-per-pixel."""
    out = image.copy()
    thickness = max(2, round(max(out.shape[:2]) / 500))
    corners = np.asarray(estimate.region_px, dtype=np.int32).reshape(-1, 1, 2)
    cv2.polylines(out, [corners], True, (255, 0, 255), thickness)
    x, y = corners[0, 0]
    cv2.putText(out, f"{estimate.cm_per_px * 10:.3f} mm/px", (int(x), max(int(y) - 10, 20)),
                cv2.FONT_HERSHEY_SIMPLEX, thickness / 2.5, (255, 0, 255), thickness)
    write_jpeg(ctx.sweep.debug_dir / f"scale_{frame}", out)
