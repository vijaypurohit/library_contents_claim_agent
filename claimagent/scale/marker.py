"""Printable scale card: an ArUco marker of known size on an A4 page.

When the card is visible in a frame, its four corners tell us exactly how many pixels make a
centimetre in that plane (see the scale stage). The 10 cm ruler lets the user confirm the printer
did not rescale the page.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from claimagent.core.settings import MarkerConfig

A4_CM = (21.0, 29.7)
DPI = 300  # print resolution used to convert centimetres to pixels


def aruco_dictionary(name: str) -> cv2.aruco.Dictionary:
    """OpenCV's predefined marker set by name, e.g. "DICT_4X4_50" (4x4 bits, 50 distinct ids)."""
    if not hasattr(cv2.aruco, name):
        raise ValueError(f"unknown ArUco dictionary {name!r}")
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))


def _px(cm: float) -> int:
    return round(cm / 2.54 * DPI)


def render_marker_page(cfg: MarkerConfig) -> Image.Image:
    if cfg.size_cm > A4_CM[0] - 2:
        raise ValueError(f"marker of {cfg.size_cm} cm does not fit on A4 with margins")
    page = Image.new("L", (_px(A4_CM[0]), _px(A4_CM[1])), 255)
    side = _px(cfg.size_cm)
    marker = cv2.aruco.generateImageMarker(aruco_dictionary(cfg.dictionary), cfg.marker_id, side)
    left, top = (page.width - side) // 2, _px(3)
    page.paste(Image.fromarray(np.asarray(marker)), (left, top))

    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=42)
    y = top + side + _px(1.5)
    draw.text((left, y), f"Marker side: {cfg.size_cm:g} cm  ({cfg.dictionary}, id {cfg.marker_id})", fill=0, font=font)
    y += _px(1.5)
    draw.line([(left, y), (left + _px(10), y)], fill=0, width=6)  # 10 cm check ruler
    for cm in range(11):
        x = left + _px(cm)
        draw.line([(x, y - 20), (x, y + 20)], fill=0, width=4)
    draw.text((left, y + _px(0.6)), "Print at 100% scale. This line must measure 10 cm.", fill=0, font=font)
    return page


def save_marker_pdf(path: Path, cfg: MarkerConfig) -> None:
    render_marker_page(cfg).save(path, "PDF", resolution=DPI)
