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
