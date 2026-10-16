"""Evaluation: a hand-built packet with known errors against hand-built truth."""

from datetime import UTC, datetime

from claimagent.domain.packet import Book, BookPrice, ClaimPacket, Room, SweepInfo
from claimagent.eval.compare import Truth, evaluate, same_title, to_markdown

NOW = datetime(2026, 10, 16, tzinfo=UTC)
TARGETS = {"book_count_error": 0.05, "title_correct": 0.7, "title_confidently_wrong": 0.03, "spine_dimension_error": 0.15,
           "book_price_error": 0.25, "items_found": 0.8, "floor_area_error": 0.1, "wall_area_error": 0.15}


def book(position, title, confidence=0.5, price=None, status="identified"):
    cost = BookPrice(amount=price, source="s", url="https://s", retrieved_at=NOW) if price else BookPrice()
    return Book(id=f"A1-{position}", unit="A", shelf="1", position=position, frame_ref="f.jpg", status=status,
                title=title, id_confidence=confidence, replacement_cost=cost)


TRUTH = Truth.model_validate({
    "verified": True, "site": "test", "book_count": 5,
    "books": [{"unit": "A", "shelf": 1, "position": i, "title": t, "legible": i != 5}
              for i, t in enumerate(["Word Power Made Easy", "Mathematics XI", "Social Science Class X", "Physics Part I",
                                     "Hindi Reader"], start=1)],
    "prices": [{"unit": "A", "shelf": 1, "position": 1, "new": 200}, {"unit": "A", "shelf": 1, "position": 2, "new": 400},
               {"unit": "A", "shelf": 1, "position": 3, "new": None}],
})


def test_title_rule():
    assert same_title("Fundamental Physics", "Pradeep's Fundamental Physics Class XII")
    assert same_title("Mathematics for Class 11", "Mathematics XI")
    assert not same_title("Mathematics for Class 11", "Mathematics Class X")   # wrong class
    assert not same_title("MATHEMATICS", "Mathematics Class X")                # one word says too little


def test_known_errors_give_the_expected_metrics():
    packet = ClaimPacket.build(SweepInfo(id="t", captured_at=NOW, country="IN", currency="INR"), room=Room(), books=[
        book(1, "Word Power Made Easy", price=210),                 # correct, price within 25%
        book(2, "Mathematics Class 8", confidence=0.95, price=900),  # confidently wrong, price far off
        book(3, None, status="unidentified"),                        # blank
        book(4, "Physics Part I"),                                   # correct
    ], items=[], review_queue=[])
    result = evaluate(packet, TRUTH, TARGETS, commit="abc123")
    m = {x.name: x for x in result.measures}
    assert m["Book count"].result.startswith("4 counted, 5 true (20% error)") and m["Book count"].met is False
    assert m["Titles correct (legible spines)"].result == "2 of 4 (50%)"
    assert m["Titles confidently wrong"].result.startswith("1 of 5 (20%)") and m["Titles confidently wrong"].met is False
    assert m["Book prices (hand-checked)"].result == "1 of 2 within target; 0 not priced"
    assert m["Spine dimensions"].met is None and "not measured" in m["Spine dimensions"].result
    assert [r.outcome for r in result.books] == ["correct", "confidently wrong", "blank", "correct", "missing"]
    assert "abc123" in to_markdown(result)
