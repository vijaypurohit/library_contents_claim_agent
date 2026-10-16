"""Geometry helpers: how two frames relate, and how boxes overlap. Pure functions, no models.

A homography is a 3x3 matrix that maps points of one image onto another image of the same flat
scene (for example a shelf front) seen from a different position. We estimate it from matching
image features, then use it to tell how much two frames overlap.
"""

from __future__ import annotations

import cv2
import numpy as np

from claimagent.domain.records import BBox
from claimagent.vision.imaging import resize_max

MATCH_SIDE = 800        # match features on downscaled copies: much faster, accurate enough for overlap
MIN_INLIERS = 15        # fewer agreeing matches than this means "no reliable relation"
_orb = cv2.ORB_create(nfeatures=2000)
_matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)


def _features(image: np.ndarray) -> tuple[list, np.ndarray | None, float]:
    """ORB keypoints and descriptors of a downscaled grey copy, plus the scale factor used."""
    small = resize_max(image, MATCH_SIDE)
    scale = image.shape[1] / small.shape[1]
    keypoints, descriptors = _orb.detectAndCompute(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), None)
    return keypoints, descriptors, scale


def estimate_homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray | None:
    """Homography mapping full-resolution `src` pixels onto `dst` pixels, or None if the frames don't match.

    Steps: find distinctive corners (ORB) in both frames, pair them by appearance, then let RANSAC
    find the homography most pairs agree with, ignoring wrong pairs.
    """
    kp_s, des_s, scale_s = _features(src)
    kp_d, des_d, scale_d = _features(dst)
    if des_s is None or des_d is None:
        return None
    matches = _matcher.match(des_s, des_d)
    if len(matches) < MIN_INLIERS:
        return None
    pts_s = np.float32([kp_s[m.queryIdx].pt for m in matches]) * scale_s
    pts_d = np.float32([kp_d[m.trainIdx].pt for m in matches]) * scale_d
    homography, inliers = cv2.findHomography(pts_s, pts_d, cv2.RANSAC, 5.0 * scale_d)
    if homography is None or int(inliers.sum()) < MIN_INLIERS:
        return None
    return homography


def overlap_fraction(homography: np.ndarray, src_shape: tuple[int, ...], dst_shape: tuple[int, ...]) -> float:
    """Share of the `dst` frame that the warped `src` frame covers (0 = none, 1 = all)."""
    h_s, w_s = src_shape[:2]
    h_d, w_d = dst_shape[:2]
    corners = np.float32([[0, 0], [w_s, 0], [w_s, h_s], [0, h_s]]).reshape(-1, 1, 2)
    warped = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
    if not cv2.isContourConvex(warped.astype(np.float32)):
        return 0.0  # a folded quadrilateral means the homography is nonsense
    frame = np.float32([[0, 0], [w_d, 0], [w_d, h_d], [0, h_d]])
    area, _ = cv2.intersectConvexConvex(warped.astype(np.float32), frame)
    return float(area) / (w_d * h_d)


def frames_overlap(a: np.ndarray, b: np.ndarray) -> float:
    """How much frame `a` covers frame `b`, from 0 to 1. Returns 0 when no reliable match exists."""
    homography = estimate_homography(a, b)
    return 0.0 if homography is None else overlap_fraction(homography, a.shape, b.shape)


def map_points(homography: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Apply a homography to an (N, 2) array of points; returns an (N, 2) array."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, np.asarray(homography, dtype=np.float64)).reshape(-1, 2)


def polygon_area(points: np.ndarray) -> float:
    """Area of a simple polygon given as (N, 2) vertices in order (shoelace formula)."""
    x, y = np.asarray(points, dtype=np.float64).T
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def local_scale(image_to_plane: np.ndarray, quad_px: np.ndarray) -> float:
    """Plane units per image pixel over a region of the image, e.g. centimetres per pixel.

    Under perspective the scale changes across the frame, so it is only meaningful for a region.
    It is the square root of (region area on the plane / region area in the image).
    """
    area_px = polygon_area(quad_px)
    if area_px <= 0:
        raise ValueError("region has no area")
    return (polygon_area(map_points(image_to_plane, quad_px)) / area_px) ** 0.5


def plane_distance(image_to_plane: np.ndarray, p: tuple[float, float], q: tuple[float, float]) -> float:
    """Distance on the plane (e.g. in cm) between two image points that lie on that plane."""
    a, b = map_points(image_to_plane, np.array([p, q]))
    return float(np.linalg.norm(a - b))


def map_box(homography: np.ndarray, box: BBox) -> BBox:
    """Box around `box` after mapping its four corners with a homography (e.g. into another frame)."""
    corners = np.array([[box.x0, box.y0], [box.x1, box.y0], [box.x1, box.y1], [box.x0, box.y1]])
    mapped = map_points(homography, corners)
    return BBox(x0=float(mapped[:, 0].min()), y0=float(mapped[:, 1].min()),
                x1=float(mapped[:, 0].max()), y1=float(mapped[:, 1].max()))


def _intersection(a: BBox, b: BBox) -> float:
    ix = max(0.0, min(a.x1, b.x1) - max(a.x0, b.x0))
    iy = max(0.0, min(a.y1, b.y1) - max(a.y0, b.y0))
    return ix * iy


def iou(a: BBox, b: BBox) -> float:
    """Intersection over union of two boxes: 1 for identical boxes, 0 for disjoint ones."""
    inter = _intersection(a, b)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def containment(a: BBox, b: BBox) -> float:
    """Share of the smaller box that lies inside the other: 1 when one box is inside the other.

    Catches a partial detection (e.g. half a spine at the frame edge) that IoU scores low.
    """
    smaller = min(a.area, b.area)
    return _intersection(a, b) / smaller if smaller > 0 else 0.0


def intersect(a: BBox, b: BBox) -> BBox | None:
    """The overlapping part of two boxes, or None if they don't overlap."""
    box = BBox(x0=max(a.x0, b.x0), y0=max(a.y0, b.y0), x1=min(a.x1, b.x1), y1=min(a.y1, b.y1))
    return box if box.width > 0 and box.height > 0 else None


def outside_share(box: BBox, width: float, height: float) -> float:
    """Share of a box's area that falls outside a width x height frame."""
    inside = _intersection(box, BBox(x0=0, y0=0, x1=width, y1=height))
    return 1 - inside / box.area if box.area > 0 else 1.0
