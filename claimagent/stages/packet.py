"""Stage 9: Packet. Assemble the claim packet, apply the user's facts, validate it and render the report.

  1. One packet line per book (inventory + identify + measure + price) and per item.
  2. User facts (runs/<id>/facts.json, see domain/facts.py) are applied: skipped shelves are left
     out, first editions and signed copies go to appraisal unpriced. A fact that matches no shelf,
     book or item goes to the review queue instead of being lost.
  3. Every review note from every stage is gathered into one review queue.
  4. Totals are computed in code from the lines (domain/packet.py); a model never writes them.
  5. The validator (report/validate.py) checks every referenced frame and snapshot exists and every
     price has quotes behind it. Problems are listed in the stage output and its warnings.
Writes: claim_packet.json, report.html and metrics.json (latency, model calls, cost per stage).
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import BaseModel, Field

from claimagent.core.context import RunContext
from claimagent.core.runner import StageEnvelope
from claimagent.core.stage import Stage, register
from claimagent.domain.facts import UserFact, load_facts
from claimagent.domain.packet import (Book, BookPrice, ClaimPacket, Dimensions, Item, ItemPrice, PriceEvidence,
                                      ReviewEntry, Room, SweepInfo, UsedValue)
from claimagent.report.render import render_report
from claimagent.report.validate import validate_packet
from claimagent.stages.detect import DetectOutput
from claimagent.stages.identify import IdentifyOutput
from claimagent.stages.inventory import InventoryOutput
from claimagent.stages.measure import MeasureOutput
from claimagent.stages.price import BookPrice as StageBookPrice
from claimagent.stages.price import ItemPrice as StageItemPrice
from claimagent.stages.price import PriceOutput, Quote, print_items
from claimagent.stages.room import RoomOutput

APPRAISAL_FACTS = {"first_edition": "the user said it is a first edition", "signed": "the user said it is signed"}


def unmatched_facts(facts: list[UserFact], inventory: InventoryOutput) -> list[UserFact]:
    """Facts that change nothing in the packet because their target is not a shelf, book or art item.

    The live agent cannot know book ids during a sweep, so it may record "the Hindi book" or no
    target at all; such a fact must reach a person rather than vanish."""
    shelves = {f"{b.unit}{b.shelf}" for b in inventory.books}
    books = {b.book_id for b in inventory.books}
    missed = []
    for fact in facts:
        if fact.kind == "skip_shelf" and fact.target not in shelves:
            missed.append(fact)
        elif fact.kind in APPRAISAL_FACTS and fact.target not in books:
            missed.append(fact)
        elif fact.kind == "print" and not print_items(inventory.items, [fact]):
            missed.append(fact)
    return missed


class PacketOutput(BaseModel):
    packet_ref: str
    report_ref: str
    metrics_ref: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    facts_applied: list[UserFact] = Field(default_factory=list)


# ---- Building lines (pure) ----

def evidence(quotes: list[Quote]) -> list[PriceEvidence]:
    return [PriceEvidence(shop=q.shop, url=q.url, amount=q.price_local, listed=f"{q.price:g} {q.currency}",
                          converted=q.converted, available=q.available, retrieved_at=q.retrieved_at, snapshot=q.snapshot_ref)
            for q in quotes]


def sourced(amount: float | None, quotes: list[Quote], kind: type[BookPrice]) -> BookPrice:
    """A packet price: the amount, its quotes, and the quote nearest the amount as the main source."""
    if amount is None or not quotes:
        return kind()
    nearest = min(quotes, key=lambda q: abs(q.price_local - amount))
    shops = sorted({q.shop for q in quotes})
    extra = {"condition_assumed": "as listed (used)"} if kind is UsedValue else {}
    return kind(amount=amount, converted=any(q.converted for q in quotes), source=f"median of {len(quotes)} listing(s): {', '.join(shops)}",
                url=nearest.url, retrieved_at=nearest.retrieved_at, snapshot=nearest.snapshot_ref, quotes=evidence(quotes), **extra)


def item_cost(price: StageItemPrice | None) -> ItemPrice:
    if price is None or price.low is None or not price.quotes:
        return ItemPrice()
    first = price.quotes[0]
    return ItemPrice(low=price.low, high=price.high, source=f"range of {len(price.quotes)} comparable listing(s)",
                     url=first.url, retrieved_at=first.retrieved_at, snapshot=first.snapshot_ref, quotes=evidence(price.quotes))


# ---- Stage ----

@register
class PacketStage(Stage):
    name = "packet"
    order = 9
    needs = ("detect", "inventory", "identify", "measure", "price", "room")
    output = PacketOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> PacketOutput:
        detect: DetectOutput = inputs["detect"]  # type: ignore[assignment]
        inventory: InventoryOutput = inputs["inventory"]  # type: ignore[assignment]
        identify: IdentifyOutput = inputs["identify"]  # type: ignore[assignment]
        measure: MeasureOutput = inputs["measure"]  # type: ignore[assignment]
        price: PriceOutput = inputs["price"]  # type: ignore[assignment]
        room: RoomOutput = inputs["room"]  # type: ignore[assignment]
        facts = load_facts(ctx.sweep.facts_path)
        review: list[ReviewEntry] = []

        ids = {b.book_id: b for b in identify.books}
        sizes = {b.book_id: b for b in measure.books}
        prices = {b.book_id: b for b in price.books}
        skipped = {f.target for f in facts if f.kind == "skip_shelf" and f.target}

        books = []
        for inv in inventory.books:
            if f"{inv.unit}{inv.shelf}" in skipped:
                review.append(ReviewEntry(ref_id=inv.book_id, reason="left out: the user asked to skip this shelf"))
                continue
            books.append(self._book(inv, ids.get(inv.book_id), sizes.get(inv.book_id), prices.get(inv.book_id),
                                    [f for f in facts if f.target == inv.book_id], review))

        item_sizes = {i.item_id: i for i in measure.items}
        item_prices = {i.item_id: i for i in price.items}
        items = []
        for inv in inventory.items:
            cost = item_prices.get(inv.item_id)
            size = item_sizes.get(inv.item_id)
            status = "unpriced" if cost is None or cost.status == "no_quotes" else cost.status
            items.append(Item(id=inv.item_id, category=inv.category,
                              description=" ".join(w for w in (inv.material, inv.label) if w), brand_model=inv.brand,
                              frame_ref=inv.frame_ref, bbox=inv.bbox,
                              dimensions_cm=Dimensions(w=size.w_cm, h=size.h_cm) if size else Dimensions(),
                              status=status, replacement_cost=item_cost(cost), confidence=size.confidence if size else None))
            for note in (cost.review if cost else []) + (size.flags if size else []):
                review.append(ReviewEntry(ref_id=inv.item_id, reason=note))

        # Sweep-level review entries: boxes not counted, frames not used, room gaps, user notes.
        for u in inventory.unconfirmed:
            review.append(ReviewEntry(ref_id=u.frame, reason=f"possible book not counted: {u.reason}"))
        for frame in inventory.unlinked_frames:
            review.append(ReviewEntry(ref_id=frame, reason="keyframe could not be linked to the others; its books were not counted"))
        for frame in detect.failed_frames:
            review.append(ReviewEntry(ref_id=frame, reason="no detector could analyse this keyframe"))
        review += [ReviewEntry(ref_id="room", reason=r) for r in room.review]
        review += [ReviewEntry(ref_id=f.target or "sweep", reason=f"user: {f.text}") for f in facts if f.kind == "note"]
        review += [ReviewEntry(ref_id=f.target or "sweep", reason=f"user said \"{f.text}\" ({f.kind}), but it matches "
                               "no shelf, book or item: apply it by hand") for f in unmatched_facts(facts, inventory)]

        manifest = ctx.sweep.read_manifest()
        sweep = SweepInfo(id=ctx.sweep.id, captured_at=manifest.created_at,
                          duration_s=max((f.t for f in manifest.frames), default=None), country=price.country,
                          currency=price.currency)
        packet = ClaimPacket.build(sweep=sweep, room=Room(**room.model_dump(include=set(Room.model_fields))),
                                   books=books, items=items, review_queue=review)
        ctx.sweep.packet_path.write_text(packet.model_dump_json(indent=2))

        errors = validate_packet(packet, ctx.sweep.root)
        for error in errors:
            ctx.warn(f"packet: {error}")
        envelopes = self._envelopes(ctx)
        shelves = [{"name": f"{s.unit}{s.shelf}", "count": s.count, "ref": s.annotated_ref}
                   for s in inventory.shelves if s.annotated_ref and f"{s.unit}{s.shelf}" not in skipped]
        crops = {b.book_id: b.crop_ref for b in identify.books if (ctx.sweep.root / b.crop_ref).exists()}
        warnings = [w for e in envelopes for w in e.warnings]
        ctx.sweep.report_path.write_text(render_report(packet, shelves, crops, warnings))
        ctx.sweep.metrics_path.write_text(json.dumps(self._metrics(envelopes, packet, errors), indent=2))
        return PacketOutput(packet_ref=ctx.sweep.packet_path.name, report_ref=ctx.sweep.report_path.name,
                            metrics_ref=ctx.sweep.metrics_path.name, valid=not errors, errors=errors, facts_applied=facts)

    @staticmethod
    def _book(inv, ident, size, cost: StageBookPrice | None, facts: list[UserFact], review: list[ReviewEntry]) -> Book:
        appraisal = [APPRAISAL_FACTS[f.kind] for f in facts if f.kind in APPRAISAL_FACTS]
        readable = ident is not None and ident.status != "unidentified"
        if not readable:
            status = "unidentified"
        elif appraisal or (cost is not None and cost.status == "needs_appraisal"):
            status = "needs_appraisal"
        else:
            status = "identified"
        priced = status == "identified" and cost is not None
        quotes = cost.quotes if cost else []
        book = Book(
            id=inv.book_id, unit=inv.unit, shelf=str(inv.shelf), position=inv.position, frame_ref=inv.frame_ref,
            bbox=inv.bbox, annotated_ref=inv.annotated_ref, status=status,
            title=ident.title if readable else None, author=ident.author if readable else None,
            edition=ident.edition if readable else None, isbn=ident.isbn if readable else None,
            spine_height_cm=size.height_cm if size else None, spine_thickness_cm=size.thickness_cm if size else None,
            dimension_confidence=size.confidence if size and size.height_cm else None,
            scale_method=size.scale_method if size and size.height_cm else None,
            id_confidence=ident.id_confidence if ident else None,
            replacement_cost=sourced(cost.replacement_cost if priced else None, [q for q in quotes if q.condition == "new"], BookPrice),
            used_value=sourced(cost.used_value if priced else None, [q for q in quotes if q.condition == "used"], UsedValue),
        )
        notes = inv.review + (ident.review if ident else []) + (size.flags if size else []) + (cost.review if cost else []) + appraisal
        review += [ReviewEntry(ref_id=inv.book_id, reason=n) for n in dict.fromkeys(notes)]
        return book

    @staticmethod
    def _envelopes(ctx: RunContext) -> list[StageEnvelope]:
        """Every earlier stage's saved output, for metrics and pipeline notes."""
        return [StageEnvelope.model_validate_json(p.read_text())
                for p in sorted(ctx.sweep.stages_dir.glob("*.json")) if not p.name.startswith("9_")]

    @staticmethod
    def _metrics(envelopes: list[StageEnvelope], packet: ClaimPacket, errors: list[str]) -> dict:
        stages = [{"stage": e.stage, "latency_s": e.latency_s, "model_calls": len(e.model_calls),
                   "billed_calls": sum(not c.cached for c in e.model_calls), "cost_usd": e.cost_usd,
                   "cost_complete": e.cost_complete} for e in sorted(envelopes, key=lambda e: e.order)]
        return {"sweep": packet.sweep.id, "stages": stages,
                "total_latency_s": round(sum(s["latency_s"] for s in stages), 2),
                "total_cost_usd": round(sum(s["cost_usd"] for s in stages), 4),
                "totals": packet.totals.model_dump(), "review_entries": len(packet.review_queue),
                "valid": not errors}
