"""Compare a sweep's claim packet with ground truth (ground_truth/<site>/truth.yaml).

Every measure that has truth data is reported, for every book and shelf of the sweep; nothing is
filtered to the frames that worked. A measure without truth data says "not measured".
Books are matched by unit, shelf and position; a title found at another position counts as
identified but mislocated.

Title outcomes, per book:
    correct            title similar to the true title (score >= 80), or every word of it (at least
                       two) inside the true title, e.g. "Fundamental Physics" for "Pradeep's
                       Fundamental Physics Class XII"; a class or part number in the system's title
                       must be the book's (leaving it out is allowed)
    blank              no title (unreadable, or not identified)
    wrong              a title that is not the true one; "confidently wrong" when the system was sure
                       (identification confidence >= 0.9, so it would not reach a reviewer)

The result is a Markdown file with each measure next to its target (config/targets.yaml) and the
git commit it ran on, so a fix can be shown as a before/after pair.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from claimagent.core.settings import PROJECT_ROOT
from claimagent.domain.packet import Book, ClaimPacket
from claimagent.text.match import core_title, numbers_agree, title_score

TITLE_CORRECT = 80.0
CONFIDENT = 0.9
TARGETS_FILE = PROJECT_ROOT / "config" / "targets.yaml"


class TruthBook(BaseModel):
    unit: str
    shelf: int
    position: int
    orientation: str | None = None
    legible: bool = True
    title: str
    author: str | None = None


class TruthPrice(BaseModel):
    unit: str
    shelf: int
    position: int
    new: float | None = None
    used: float | None = None
    currency: str = "INR"
    url: str | None = None


class TruthItem(BaseModel):
    category: str
    description: str | None = None


class Truth(BaseModel):
    verified: bool = False
    site: str
    sweeps: list[str] = Field(default_factory=list)
    book_count: int
    books: list[TruthBook] = Field(default_factory=list)
    spines_cm: list[dict] = Field(default_factory=list)
    prices: list[TruthPrice] = Field(default_factory=list)
    items: list[TruthItem] = Field(default_factory=list)
    room: dict = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Truth:
        return cls.model_validate(yaml.safe_load(path.read_text()))


class Measure(BaseModel):
    name: str
    result: str                      # human-readable value, or "not measured"
    target: str
    met: bool | None                 # None when not measured


class BookRow(BaseModel):
    position: str
    true_title: str
    legible: bool
    system_title: str | None
    outcome: str                     # correct / blank / wrong / confidently wrong / missing
    located: bool
    true_price: float | None
    system_price: float | None


class Evaluation(BaseModel):
    sweep: str
    commit: str
    evaluated_at: datetime
    truth_verified: bool
    measures: list[Measure]
    books: list[BookRow]


def _key(unit: str, shelf: int | str, position: int | None) -> str:
    return f"{unit}{shelf}-{position}"


def same_title(system: str, true: str) -> bool:
    """The evaluation's notion of a correct title (see the module docstring)."""
    if not numbers_agree(system, true):
        return False          # a class or part the book does not have (leaving the class out is not wrong)
    words = core_title(system).split()
    contained = len(words) >= 2 and set(words) <= set(core_title(true).split())
    return title_score(system, true) >= TITLE_CORRECT or contained


def title_outcome(book: Book | None, truth: TruthBook) -> str:
    if book is None:
        return "missing"
    if not book.title:
        return "blank"
    if same_title(book.title, truth.title):
        return "correct"
    return "confidently wrong" if (book.id_confidence or 0) >= CONFIDENT else "wrong"


def evaluate(packet: ClaimPacket, truth: Truth, targets: dict, commit: str) -> Evaluation:
    by_position = {_key(b.unit or "", b.shelf or "", b.position): b for b in packet.books}
    prices = {_key(p.unit, p.shelf, p.position): p for p in truth.prices}
    rows = []
    for t in truth.books:
        key = _key(t.unit, t.shelf, t.position)
        book = by_position.get(key)
        outcome = title_outcome(book, t)
        located = outcome == "correct"
        if not located:
            # The title may be right but at another position: identified, mislocated.
            located_elsewhere = any(b.title and same_title(b.title, t.title) for b in packet.books)
            if located_elsewhere and outcome != "correct":
                outcome = f"{outcome} (title found at another position)"
        rows.append(BookRow(position=key, true_title=t.title, legible=t.legible,
                            system_title=book.title if book else None, outcome=outcome, located=located,
                            true_price=prices[key].new if key in prices else None,
                            system_price=book.replacement_cost.amount if book else None))

    measures = []
    # Book count.
    error = abs(packet.totals.book_count - truth.book_count) / truth.book_count
    measures.append(Measure(name="Book count", result=f"{packet.totals.book_count} counted, {truth.book_count} true ({error:.0%} error)",
                            target=f"within {targets['book_count_error']:.0%}", met=error <= targets["book_count_error"]))
    # Titles.
    legible = [r for r in rows if r.legible]
    correct = sum(r.outcome == "correct" for r in legible)
    confident_wrong = sum(r.outcome.startswith("confidently wrong") for r in rows)
    wrong = sum(r.outcome.startswith("wrong") for r in rows)
    blank = sum(r.outcome.startswith("blank") or r.outcome == "missing" for r in rows)
    share = correct / len(legible) if legible else 0.0
    measures.append(Measure(name="Titles correct (legible spines)", result=f"{correct} of {len(legible)} ({share:.0%})",
                            target=f"at least {targets['title_correct']:.0%}", met=share >= targets["title_correct"]))
    cw_share = confident_wrong / len(rows) if rows else 0.0
    measures.append(Measure(name="Titles confidently wrong", result=f"{confident_wrong} of {len(rows)} ({cw_share:.0%}); "
                            f"{wrong} other wrong (sent to review), {blank} blank",
                            target=f"at most {targets['title_confidently_wrong']:.0%}", met=cw_share <= targets["title_confidently_wrong"]))
    # Spine dimensions.
    measures.append(_not_measured("Spine dimensions", f"within {targets['spine_dimension_error']:.0%}") if not truth.spines_cm
                    else _dimensions(packet, truth, targets))
    # Prices.
    measures += _prices(rows, targets)
    # Items.
    if truth.items:
        found = [t for t in truth.items if any(i.category == t.category for i in packet.items)]
        share_items = len(found) / len(truth.items)
        measures.append(Measure(name="Non-book items found (right category)", result=f"{len(found)} of {len(truth.items)} ({share_items:.0%})",
                                target=f"at least {targets['items_found']:.0%}", met=share_items >= targets["items_found"]))
    else:
        measures.append(_not_measured("Non-book items", f"at least {targets['items_found']:.0%}"))
    # Room.
    for name, field, key in (("Floor area", "floor_area_m2", "floor_area_error"), ("Wall area", "wall_area_m2", "wall_area_error")):
        true_value = truth.room.get(field)
        system_value = getattr(packet.room, field)
        if true_value is None:
            measures.append(_not_measured(name, f"within {targets[key]:.0%}",
                                          f"system: {system_value if system_value is not None else 'empty'}"))
        else:
            err = abs((system_value or 0) - true_value) / true_value if system_value is not None else None
            measures.append(Measure(name=name, result=f"{system_value} m² vs {true_value} m²" + (f" ({err:.0%})" if err is not None else ""),
                                    target=f"within {targets[key]:.0%}", met=err is not None and err <= targets[key]))
    return Evaluation(sweep=packet.sweep.id, commit=commit, evaluated_at=datetime.now(UTC), truth_verified=truth.verified,
                      measures=measures, books=rows)


def _not_measured(name: str, target: str, note: str = "") -> Measure:
    return Measure(name=name, result="not measured: no ground truth" + (f" ({note})" if note else ""), target=target, met=None)


def _dimensions(packet: ClaimPacket, truth: Truth, targets: dict) -> Measure:
    books = {_key(b.unit or "", b.shelf or "", b.position): b for b in packet.books}
    errors, missing = [], 0
    for s in truth.spines_cm:
        b = books.get(_key(s["unit"], s["shelf"], s["position"]))
        if b is None or b.spine_height_cm is None:
            missing += 1
            continue
        errors.append(abs(b.spine_height_cm - s["height"]) / s["height"])
    within = sum(e <= targets["spine_dimension_error"] for e in errors)
    return Measure(name="Spine heights", result=f"{within} of {len(truth.spines_cm)} within target, {missing} not measured",
                   target=f"within {targets['spine_dimension_error']:.0%}", met=within == len(truth.spines_cm))


def _prices(rows: list[BookRow], targets: dict) -> list[Measure]:
    target = f"within {targets['book_price_error']:.0%}"
    known = [r for r in rows if r.true_price is not None]
    if not known and not any(r.true_price is None for r in rows):
        return [_not_measured("Book prices", target)]
    priced = [r for r in known if r.system_price is not None]
    within = [r for r in priced if abs(r.system_price - r.true_price) / r.true_price <= targets["book_price_error"]]
    unknown = [r for r in rows if r.true_price is None]
    left_blank = sum(r.system_price is None for r in unknown)
    return [
        Measure(name="Book prices (hand-checked)", result=f"{len(within)} of {len(known)} within target; "
                f"{len(known) - len(priced)} not priced", target=target, met=bool(known) and len(within) == len(known)),
        Measure(name="Books with no listing anywhere left unpriced", result=f"{left_blank} of {len(unknown)}",
                target="all", met=left_blank == len(unknown)),
    ]


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=PROJECT_ROOT,
                               capture_output=True, text=True).stdout.strip()
        return f"{sha}{'+changes' if dirty else ''}" or "unknown"
    except OSError:
        return "unknown"


def load_targets() -> dict:
    return yaml.safe_load(TARGETS_FILE.read_text())


def to_markdown(e: Evaluation) -> str:
    mark = {True: "met", False: "**missed**", None: "–"}
    lines = [f"# Evaluation: sweep `{e.sweep}`", "",
             f"Commit `{e.commit}` · {e.evaluated_at:%Y-%m-%d %H:%M} UTC · ground truth {'verified' if e.truth_verified else 'NOT verified'}", "",
             "| Measure | Result | Target | |", "|---|---|---|---|"]
    lines += [f"| {m.name} | {m.result} | {m.target} | {mark[m.met]} |" for m in e.measures]
    lines += ["", "## Books", "", "| Position | True title | Legible | System title | Outcome | True price | System price |",
              "|---|---|---|---|---|---|---|"]
    for r in e.books:
        lines.append(f"| {r.position} | {r.true_title} | {'yes' if r.legible else 'no'} | {r.system_title or '–'} | {r.outcome} | "
                     f"{r.true_price if r.true_price is not None else '–'} | {r.system_price if r.system_price is not None else '–'} |")
    return "\n".join(lines) + "\n"
