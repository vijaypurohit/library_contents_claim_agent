"""Packet stage: lines from stage outputs, user facts, totals, validator, report and metrics."""

import json
from datetime import UTC, datetime

from claimagent.core.context import RunContext
from claimagent.domain.records import BBox
from claimagent.report.validate import validate_packet
from claimagent.stages.detect import DetectOutput
from claimagent.stages.identify import IdentifiedBook, IdentifyOutput
from claimagent.stages.inventory import InventoryBook, InventoryOutput, View
from claimagent.stages.measure import MeasuredBook, MeasureOutput
from claimagent.stages.packet import PacketStage
from claimagent.stages.price import BookPrice, PriceOutput, Quote
from claimagent.stages.room import RoomOutput
from claimagent.domain.packet import ClaimPacket

NOW = datetime(2026, 10, 16, tzinfo=UTC)
BOX = BBox(x0=0, y0=0, x1=10, y1=30)


def run(ctx: RunContext, facts=None, snapshot="evidence/prices/p.html"):
    frame = ctx.sweep.frames()[0].name
    (ctx.sweep.evidence_dir / "prices").mkdir(exist_ok=True)
    (ctx.sweep.evidence_dir / "prices" / "p.html").write_text("<html>₹200</html>")
    if facts is not None:
        ctx.sweep.facts_path.write_text(json.dumps(facts))
    books = [InventoryBook(book_id=i, unit="A", shelf=int(i[1]), position=int(i[-1]), orientation="upright",
                           frame_ref=frame, bbox=BOX, plane_box=BOX, views=[View(frame=frame, bbox=BOX, truncated=False)],
                           annotated_ref=None, review=[]) for i in ("A1-1", "A1-2", "A2-1")]
    ident = [IdentifiedBook(book_id="A1-1", frame_ref=frame, crop_ref="x", spine_text="t", legible=True,
                            status="identified", title="Word Power Made Easy", author="Norman Lewis", id_confidence=1.0),
             IdentifiedBook(book_id="A1-2", frame_ref=frame, crop_ref="x", spine_text="", legible=False, status="unidentified"),
             IdentifiedBook(book_id="A2-1", frame_ref=frame, crop_ref="x", spine_text="t", legible=True,
                            status="spine_only", title="Sanjiv English Course", id_confidence=0.5)]
    quote = Quote(shop="flipkart", url="https://fk/p", title="Word Power Made Easy", condition="new", price=200.0,
                  currency="INR", price_local=200.0, match_score=100, retrieved_at=NOW, snapshot_ref=snapshot)
    prices = [BookPrice(book_id="A1-1", status="priced", currency="INR", replacement_cost=200.0, quotes=[quote]),
              BookPrice(book_id="A1-2", status="not_priced", currency="INR"),
              BookPrice(book_id="A2-1", status="no_quotes", currency="INR", review=["no matching listing"])]
    inputs = {
        "detect": DetectOutput(frames=[], failed_frames=[]),
        "inventory": InventoryOutput(reference_frame=frame, books=books, items=[], shelves=[], unconfirmed=[], unlinked_frames=[]),
        "identify": IdentifyOutput(reader="fake", books=ident),
        "measure": MeasureOutput(shelves=[], books=[MeasuredBook(book_id="A1-1", height_cm=21.0, thickness_cm=2.5,
                                                                  scale_method="catalogue_height", confidence=0.6)], items=[]),
        "price": PriceOutput(country="IN", currency="INR", books=prices, items=[], blocked_shops={}),
        "room": RoomOutput(review=["fewer than two walls measured: room size unknown"]),
    }
    out = PacketStage().run(ctx, inputs)
    return out, ClaimPacket.model_validate_json(ctx.sweep.packet_path.read_text())


def test_packet_lines_totals_and_files(ctx: RunContext):
    out, packet = run(ctx)
    assert out.valid and out.errors == []
    assert [b.status for b in packet.books] == ["identified", "unidentified", "identified"]
    assert packet.books[1].title is None
    assert packet.totals.books_replacement_cost == 200.0 and packet.totals.excluded_from_totals == 2
    assert packet.totals.shelf_run_m == 0.025
    assert packet.books[0].replacement_cost.quotes[0].listed == "200 INR"
    reasons = {(e.ref_id, e.reason) for e in packet.review_queue}
    assert ("A2-1", "no matching listing") in reasons and any(r[0] == "room" for r in reasons)
    assert ctx.sweep.report_path.exists() and "Word Power Made Easy" in ctx.sweep.report_path.read_text()
    assert json.loads(ctx.sweep.metrics_path.read_text())["valid"] is True


def test_facts_skip_a_shelf_and_send_a_signed_book_to_appraisal(ctx: RunContext):
    facts = [{"kind": "skip_shelf", "target": "A2", "text": "skip the bottom shelf"},
             {"kind": "signed", "target": "A1-1", "text": "that one is signed by the author"}]
    out, packet = run(ctx, facts)
    assert [b.id for b in packet.books] == ["A1-1", "A1-2"]
    assert packet.books[0].status == "needs_appraisal" and packet.books[0].replacement_cost.amount is None
    assert packet.totals.books_replacement_cost == 0.0
    assert len(out.facts_applied) == 2
    assert any("skip this shelf" in e.reason for e in packet.review_queue)


def test_validator_finds_a_missing_snapshot_and_frame(ctx: RunContext):
    out, packet = run(ctx, snapshot="evidence/prices/missing.html")
    assert not out.valid and any("missing file evidence/prices/missing.html" in e for e in out.errors)
    broken = packet.model_copy(update={"books": [packet.books[0].model_copy(update={"frame_ref": "gone.jpg"})] + packet.books[1:]})
    assert any("frames/gone.jpg" in e for e in validate_packet(broken, ctx.sweep.root))


def test_validator_finds_edited_totals(ctx: RunContext):
    _, packet = run(ctx)
    edited = packet.model_copy(update={"totals": packet.totals.model_copy(update={"books_replacement_cost": 999.0})})
    assert "totals do not equal the sum of the lines" in validate_packet(edited, ctx.sweep.root)
