"""Image quality measures. Pure functions: image in, number out.

Used by the keyframe stage (pick the sharpest frames) and later by the live agent
(tell the user to slow down when frames get blurry).
"""

from __future__ import annotations

import cv2
import numpy as np

from claimagent.vision.imaging import resize_max

ANALYSIS_SIDE = 1024  # measure at a fixed size so 4K and 720p frames get comparable scores


def sharpness(image: np.ndarray) -> float:
    """Variance of the Laplacian: high when the image has crisp edges, low when blurred.

    The Laplacian responds to rapid brightness changes (edges, text). Motion blur and bad focus
    smear those edges out, so the spread (variance) of the response drops.
    """
    gray = cv2.cvtColor(resize_max(image, ANALYSIS_SIDE), cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def glare(image: np.ndarray, level: int = 245) -> float:
    """Share of pixels that are blown out (almost white): reflections on glossy covers or glass."""
    gray = cv2.cvtColor(resize_max(image, ANALYSIS_SIDE), cv2.COLOR_BGR2GRAY)
    return float((gray >= level).mean())


def motion(previous: np.ndarray, current: np.ndarray) -> float:
    """How much the view changed between two frames, 0 (still) to 1: the mean absolute difference
    of small grey copies. A fast pan scores high and blurs spines."""
    a = cv2.cvtColor(cv2.resize(previous, (160, 120)), cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(cv2.resize(current, (160, 120)), cv2.COLOR_BGR2GRAY).astype(np.float32)
    return float(np.abs(a - b).mean() / 255.0)
