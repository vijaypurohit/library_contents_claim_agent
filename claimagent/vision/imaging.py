"""Image helpers shared by all stages: load, save, resize, crop, rotate, draw boxes, contact sheets.

Images are NumPy arrays in OpenCV's BGR colour order throughout the code base.
This module holds no model calls and no stage logic, so every function is easy to unit test.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from claimagent.domain.records import BBox

register_heif_opener()  # lets Pillow open iPhone/Android HEIC photos


def read_image(path: Path) -> np.ndarray:
    """Load an image as BGR, applying EXIF orientation. Supports HEIC."""
    with Image.open(path) as pil:
        oriented = ImageOps.exif_transpose(pil).convert("RGB")
    return cv2.cvtColor(np.asarray(oriented), cv2.COLOR_RGB2BGR)


def resize_max(image: np.ndarray, max_side: int) -> np.ndarray:
    """Shrink so the longest side is at most `max_side`; never enlarges. Returns the input if already small."""
    h, w = image.shape[:2]
    scale = max_side / max(h, w)
    if scale >= 1:
        return image
    return cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def encode_jpeg(image: np.ndarray, quality: int = 90, max_side: int | None = None) -> bytes:
    """JPEG bytes, optionally downscaled first: the form in which images are sent to models."""
    if max_side:
        image = resize_max(image, max_side)
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()


def write_jpeg(path: Path, image: np.ndarray, quality: int = 90) -> None:
    path.write_bytes(encode_jpeg(image, quality))


def crop(image: np.ndarray, box: BBox, pad: float = 0.0) -> np.ndarray:
    """Crop a box, padded by a fraction of its size, clipped to the image."""
    h, w = image.shape[:2]
    px, py = box.width * pad, box.height * pad
    x0, y0 = max(int(box.x0 - px), 0), max(int(box.y0 - py), 0)
    x1, y1 = min(int(round(box.x1 + px)), w), min(int(round(box.y1 + py)), h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"box {box} lies outside the {w}x{h} image")
    return image[y0:y1, x0:x1].copy()


def rotate90(image: np.ndarray, quarter_turns: int) -> np.ndarray:
    """Rotate counter-clockwise by 90 degrees per quarter turn."""
    return np.ascontiguousarray(np.rot90(image, quarter_turns % 4))


def draw_boxes(image: np.ndarray, boxes: Sequence[BBox], labels: Sequence[str] | None = None,
               color: tuple[int, int, int] = (0, 200, 255)) -> np.ndarray:
    """A copy of `image` with each box outlined and labelled (by default 1, 2, 3, ...)."""
    out = image.copy()
    thickness = max(2, round(max(out.shape[:2]) / 500))  # scale line width with image size so 4K stays readable
    font_scale = thickness / 2.5
    for i, box in enumerate(boxes):
        p0, p1 = (int(box.x0), int(box.y0)), (int(box.x1), int(box.y1))
        cv2.rectangle(out, p0, p1, color, thickness)
        label = labels[i] if labels else str(i + 1)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        cv2.rectangle(out, p0, (p0[0] + tw + 6, p0[1] + th + 8), color, -1)
        cv2.putText(out, label, (p0[0] + 3, p0[1] + th + 4), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 0), thickness)
    return out


def contact_sheet(images: Sequence[np.ndarray], captions: Sequence[str] | None = None,
                  thumb_width: int = 320, columns: int = 4) -> np.ndarray:
    """Thumbnails in a grid, for checking many frames at a glance."""
    if not images:
        raise ValueError("no images for contact sheet")
    thumbs = []
    for i, img in enumerate(images):
        h, w = img.shape[:2]
        thumb = cv2.resize(img, (thumb_width, round(h * thumb_width / w)), interpolation=cv2.INTER_AREA)
        if captions:
            cv2.putText(thumb, captions[i], (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        thumbs.append(thumb)
    cell_h = max(t.shape[0] for t in thumbs)
    rows = -(-len(thumbs) // columns)
    sheet = np.full((rows * cell_h, columns * thumb_width, 3), 255, dtype=np.uint8)
    for i, thumb in enumerate(thumbs):
        r, c = divmod(i, columns)
        sheet[r * cell_h:r * cell_h + thumb.shape[0], c * thumb_width:(c + 1) * thumb_width] = thumb
    return sheet
