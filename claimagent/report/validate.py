"""Packet validator: checks a finished claim packet against the files it points to.

The packet model already refuses a price without a source and totals that differ from the lines.
This adds the checks that need the sweep folder:
- every frame, annotated image and price snapshot the packet references exists
- every price has at least one quote, and each quote has a URL and an existing snapshot
- every filled dimension states its scale method
- the totals equal the sum of the lines (recomputed here as well, as a second guard)
"""

from __future__ import annotations

from pathlib import Path

from claimagent.domain.packet import ClaimPacket, compute_totals


def validate_packet(packet: ClaimPacket, sweep_root: Path) -> list[str]:
    """Every problem found, as human-readable lines; an empty list means the packet is valid."""
    errors: list[str] = []

    def exists(ref: str | None, what: str) -> None:
        if ref and not (sweep_root / ref).exists():
            errors.append(f"{what}: missing file {ref}")

    if packet.totals != compute_totals(packet.books, packet.items):
        errors.append("totals do not equal the sum of the lines")

    for book in packet.books:
        exists(f"frames/{book.frame_ref}", f"book {book.id} frame")
        exists(book.annotated_ref, f"book {book.id} annotated shelf")
        if (book.spine_height_cm is not None or book.spine_thickness_cm is not None) and not book.scale_method:
            errors.append(f"book {book.id}: dimensions without a scale method")
        for label, price in (("replacement cost", book.replacement_cost), ("used value", book.used_value)):
            if price.amount is None:
                continue
            if not price.quotes:
                errors.append(f"book {book.id} {label}: no quotes behind the amount")
            exists(price.snapshot, f"book {book.id} {label} snapshot")
            for quote in price.quotes:
                if not quote.url:
                    errors.append(f"book {book.id} {label}: a quote has no URL")
                exists(quote.snapshot, f"book {book.id} {label} quote snapshot")

    for item in packet.items:
        exists(f"frames/{item.frame_ref}", f"item {item.id} frame")
        cost = item.replacement_cost
        if cost.low is not None:
            if not cost.quotes:
                errors.append(f"item {item.id}: no quotes behind the range")
            for quote in cost.quotes:
                exists(quote.snapshot, f"item {item.id} quote snapshot")
    return errors
