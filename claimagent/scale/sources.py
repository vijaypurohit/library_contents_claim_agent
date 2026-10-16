"""Scale sources: ways to learn how many centimetres one pixel covers on a flat surface.

Every source returns the same record, a `ScaleEstimate`, so the ladder (`scale/ladder.py`) can
compare and choose between them without knowing how each one works. The rungs, best first:

    card              printed ArUco marker of known side (this module, used by the scale stage)
    catalogue_height  printed heights of identified books on the same shelf (measure stage)
    known_object      objects of standard size found by detect, e.g. A4 paper (measure stage)
    depth             depth model plus focal length (measure stage, optional)

Only the card is decided here, because the other rungs need detections and identifications that
exist only later in the pipeline. A plain A4 sheet without a marker is not handled here: its
outline alone cannot be measured reliably under perspective, so it is a known object instead.
"""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np
from pydantic import BaseModel

from claimagent.core.settings import MarkerConfig
from claimagent.scale.marker import aruco_dictionary
from claimagent.vision.geometry import local_scale

Method = Literal["card", "catalogue_height", "known_object", "depth"]

# A marker this many pixels wide or more is measured to well under 1%: corner error is about half a pixel.
FULL_CONFIDENCE_SIDE_PX = 100
MIN_CONFIDENCE = 0.3
CARD_CONFIDENCE = 0.95           # the best a card gets; a printing error of a few percent is still possible
SKEW_LIMIT = 0.5                 # shortest / longest marker edge below this means a steep, less precise view
SKEW_PENALTY = 0.8


class ScaleEstimate(BaseModel):
    """How big things are on one plane of one frame, according to one source."""

    method: Method
    cm_per_px: float                       # at `region_px`; under perspective it varies across the frame
    confidence: float                      # 0-1
    plane_id: str | None = None            # which physical surface, e.g. "marker-0"
    image_to_plane: list[list[float]] | None = None  # 3x3 homography: image pixels -> plane centimetres
    region_px: list[list[float]] | None = None       # where the scale was measured (e.g. marker corners)
    propagated_from: str | None = None     # frame the scale was carried over from, if not measured here
    detail: str = ""                       # human-readable note on how it was obtained


# Known-object sizes: long side, short side in cm, and how far the object's size can be trusted.
# Power sockets and light switches are not listed: their plates differ between countries and makers.
KNOWN_OBJECTS: dict[str, tuple[float, float | None, float]] = {
    "a4_paper": (29.7, 21.0, 0.7),
    "credit_card": (8.56, 5.4, 0.7),
    "door": (205.0, None, 0.3),   # interior doors are roughly 200-210 cm tall; only the height is used
}
NOT_IN_PLANE = 0.7                # a reference object is rarely in exactly the same plane as the books


def known_object_scale(reference_type: str, long_px: float, short_px: float) -> ScaleEstimate | None:
    """Scale from a detected object of standard size; None for objects of unknown or variable size."""
    if reference_type not in KNOWN_OBJECTS or long_px <= 0:
        return None
    long_cm, short_cm, trust = KNOWN_OBJECTS[reference_type]
    ratios = [long_cm / long_px] + ([short_cm / short_px] if short_cm and short_px > 0 else [])
    cm_per_px = sum(ratios) / len(ratios)
    # Both sides agreeing means the object is seen nearly face-on; disagreeing means a slanted view.
    if len(ratios) == 2 and abs(ratios[0] - ratios[1]) / cm_per_px > 0.15:
        trust *= 0.6
    return ScaleEstimate(method="known_object", cm_per_px=cm_per_px, confidence=round(trust * NOT_IN_PLANE, 3),
                         detail=f"{reference_type.replace('_', ' ')} of standard size")


class CardScale:
    """Finds the printed scale card (see `claim marker`) and measures its plane exactly.

    The four marker corners and the known side length give a homography from image pixels to
    centimetres on the card's plane, which also holds for anything else lying in that plane.
    """

    method: Method = "card"

    def __init__(self, cfg: MarkerConfig):
        self.cfg = cfg
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX  # sub-pixel corners: needed for 1% accuracy
        self._detector = cv2.aruco.ArucoDetector(aruco_dictionary(cfg.dictionary), params)

    def find_corners(self, image: np.ndarray) -> np.ndarray | None:
        """Pixel corners of our marker as a (4, 2) array, in the marker's own order
        (top-left, top-right, bottom-right, bottom-left), or None if it is not visible."""
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        corners, ids, _ = self._detector.detectMarkers(grey)
        if ids is None:
            return None
        for quad, marker_id in zip(corners, ids.ravel()):
            if int(marker_id) == self.cfg.marker_id:  # other ids have no known size
                return quad.reshape(4, 2).astype(np.float64)
        return None

    def estimate(self, image: np.ndarray) -> ScaleEstimate | None:
        corners = self.find_corners(image)
        if corners is None:
            return None
        side = self.cfg.size_cm
        plane = np.array([[0, 0], [side, 0], [side, side], [0, side]], dtype=np.float64)
        homography = cv2.getPerspectiveTransform(corners.astype(np.float32), plane.astype(np.float32))

        edges = np.linalg.norm(corners - np.roll(corners, -1, axis=0), axis=1)
        # Small markers have proportionally larger corner errors; steep views stretch those errors further.
        confidence = CARD_CONFIDENCE * min(1.0, max(MIN_CONFIDENCE, edges.min() / FULL_CONFIDENCE_SIDE_PX))
        if edges.min() / edges.max() < SKEW_LIMIT:
            confidence *= SKEW_PENALTY
        return ScaleEstimate(
            method="card",
            cm_per_px=local_scale(homography, corners),
            confidence=round(confidence, 3),
            plane_id=f"marker-{self.cfg.marker_id}",
            image_to_plane=homography.tolist(),
            region_px=corners.tolist(),
            detail=f"{side:g} cm ArUco marker, smallest edge {edges.min():.0f} px",
        )
