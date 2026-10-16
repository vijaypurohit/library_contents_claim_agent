"""Reading physical sizes written as text, e.g. catalogue dimensions."""

from __future__ import annotations

import re

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_TO_CM = {"cm": 1.0, "centimeter": 1.0, "centimetre": 1.0, "mm": 0.1, "in": 2.54, "inch": 2.54, '"': 2.54}
MIN_BOOK_CM, MAX_BOOK_CM = 8.0, 60.0   # outside this range a "height" is a data-entry error


def book_height_cm(text: str | None) -> float | None:
    """A book's height from catalogue dimensions, or None if it can't be read.

    Accepts "24.00 cm", "10,5x18 cm", "24 x 16 x 2 centimeters", "6.8 x 3.9 x 1.3 inches".
    The height is taken as the largest number: for a book it is the longest side.
    """
    if not text:
        return None
    lowered = text.lower()
    numbers = [float(n.replace(",", ".")) for n in _NUMBER.findall(lowered)]
    unit = next((factor for name, factor in sorted(_TO_CM.items(), key=lambda kv: -len(kv[0])) if name in lowered), None)
    if not numbers or unit is None:
        return None
    height = max(numbers) * unit
    return round(height, 1) if MIN_BOOK_CM <= height <= MAX_BOOK_CM else None
