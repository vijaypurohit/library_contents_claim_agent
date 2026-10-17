"""Stage 5: Identify. Read each book's spine, then find the book in public catalogues.

  1. Crop each book from its best view (inventory's `frame_ref`) at full resolution. Spine text can
     run along the spine either way, or across it, so an upright spine is shown three times in one
     image: as is, and turned a quarter each way (a flat book: as is and upside down). One copy
     always reads upright. The images,
     numbered, go about 5 per call to the "read" model providers (prompt: prompts/read_spines.md).
     The model reports only visible text and whether the spine is legible.
  2. Search the catalogues (Open Library, Google Books) with the title and author read, and score
     every candidate with text/match.py:
        score >= 90           identified
        75 <= score < 90      identified, and sent to review
        legible, no match     spine_only: the spine text is kept as the title, no ISBN, sent to review
        not legible           unidentified: no title, whatever the model returned
  3. Edition and ISBN are set only when the publisher on the spine matches exactly one edition.
     The same spine text read for several different books is distrusted: weak readers copy text from
     one image to the next on blurry views. Those books are left unidentified and sent to review
     (a person can confirm them if they really are copies of one book).
  4. The printed height is kept for the measure stage's catalogue-height scale: from the chosen
     edition, or from several editions that agree within 10% when no single edition is known.

Output: one record per book; debug/spine_<book_id>.jpg shows exactly what the model was shown.
"""

from __future__ import annotations

import statistics
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from typing import Literal

import cv2
import numpy as np

from pydantic import BaseModel, Field

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.domain.records import CatalogueRecord
from claimagent.prompts import load_prompt
from claimagent.providers.base import Catalogue, CatalogueError, ProviderUnavailable
from claimagent.stages.inventory import InventoryBook, InventoryOutput
from claimagent.text.match import IDENTIFIED, REVIEW, book_score, core_title, normalise, publisher_matches
from claimagent.vision.imaging import crop, encode_jpeg, read_image, rotate90, write_jpeg

CROP_PAD = 0.06          # a little margin around the box, so text at the box edge is not cut off
SEND_MAX_SIDE = 1280     # long side of each crop sent to the model
HEIGHT_AGREEMENT = 0.10  # editions' heights must agree within this share to be used without a known edition
SPINE_ONLY_CONFIDENCE = 0.5
IDENTIFY_PARALLEL = 4    # spine-reading batches in flight at once (detect runs 4 at once too)
GAP_PX = 24              # white space between the two copies in a reading view

Status = Literal["identified", "spine_only", "unidentified"]


# ---- What the model is asked to return ----

class SpineReading(BaseModel):
    index: int
    legible: bool
    text: str = ""
    title: str | None = None
    author: str | None = None
    publisher: str | None = None
    script: str | None = None


class ReadAnswer(BaseModel):
    spines: list[SpineReading] = Field(default_factory=list)


# ---- What this stage writes ----

class IdentifiedBook(BaseModel):
    book_id: str
    frame_ref: str
    crop_ref: str                         # the crop the model read, under debug/
    spine_text: str                       # everything readable on the spine, as printed
    legible: bool
    status: Status
    title: str | None = None
    author: str | None = None
    publisher: str | None = None
    edition: str | None = None            # only when one catalogue edition matches the spine's publisher
    isbn: str | None = None               # ditto
    source: str | None = None             # "openlibrary", "googlebooks", or "spine" when only read
    source_url: str | None = None
    match_score: float | None = None      # 0-100, best catalogue candidate
    id_confidence: float = 0.0            # 0-1
    catalogue_height_cm: float | None = None
    height_basis: str | None = None       # "edition", or "<n> editions agree"
    review: list[str] = Field(default_factory=list)


class IdentifyOutput(BaseModel):
    reader: str | None                    # provider chain that read the spines
    books: list[IdentifiedBook]


# ---- Pure decision logic (tested without models or network) ----

def status_for(legible: bool, score: float | None) -> Status:
    if not legible:
        return "unidentified"
    if score is not None and score >= REVIEW:
        return "identified"
    return "spine_only"


def score_candidate(reading: SpineReading, record: CatalogueRecord) -> float:
    """Best score of a record against what was read, with and without the record's subtitle."""
    title = reading.title or reading.text
    full = f"{record.title} {record.subtitle}" if record.subtitle else record.title
    return max(book_score(title, reading.author, record.title, record.authors, reading.text),
               book_score(title, reading.author, full, record.authors, reading.text))


def unique_edition(publisher: str | None, editions: list[CatalogueRecord]) -> CatalogueRecord | None:
    """The one edition whose publisher matches the spine's publisher, or None if zero or several match."""
    matching = [e for e in editions if publisher_matches(publisher, e.publisher)]
    return matching[0] if len(matching) == 1 else None


def agreed_height(editions: list[CatalogueRecord]) -> tuple[float, int] | None:
    """Median printed height of the editions that record one, if they agree within HEIGHT_AGREEMENT."""
    heights = [e.height_cm for e in editions if e.height_cm]
    if not heights:
        return None
    median = statistics.median(heights)
    if (max(heights) - min(heights)) / median > HEIGHT_AGREEMENT:
        return None
    return round(median, 1), len(heights)


def _stack(copies: list[np.ndarray]) -> np.ndarray:
    """Images one above the other, left-aligned on white, with a gap between them."""
    width = max(c.shape[1] for c in copies)
    rows = []
    for copy in copies:
        padded = np.full((copy.shape[0], width, 3), 255, np.uint8)
        padded[:, :copy.shape[1]] = copy
        rows += [padded, np.full((GAP_PX, width, 3), 255, np.uint8)]
    return np.vstack(rows[:-1])


def reading_view(patch: np.ndarray, orientation: str, number: int) -> np.ndarray:
    """One image showing the spine in every orientation its text may need, with the image number
    printed top-left so the model can't mix images up.

    Upright spine: the crop as is on the left (text printed across the spine), and on the right the
    crop turned clockwise above the crop turned anticlockwise (text running along it either way).
    Flat book: as is above upside down."""
    if orientation == "upright":
        turned = _stack([rotate90(patch, -1), rotate90(patch, 1)])
        height = max(patch.shape[0], turned.shape[0])
        body = np.full((height, patch.shape[1] + GAP_PX + turned.shape[1], 3), 255, np.uint8)
        body[:patch.shape[0], :patch.shape[1]] = patch
        body[:turned.shape[0], patch.shape[1] + GAP_PX:] = turned
    else:
        body = _stack([patch, rotate90(patch, 2)])
    label_h = max(40, body.shape[1] // 25)
    label = np.full((label_h, body.shape[1], 3), 255, np.uint8)
    cv2.putText(label, str(number), (8, label_h - 10), cv2.FONT_HERSHEY_SIMPLEX, label_h / 30, (0, 0, 0), 2)
    return np.vstack([label, body])


def repeated_readings(readings: dict[str, SpineReading]) -> dict[str, list[str]]:
    """Books whose legible spine text is identical to another book's, mapped to the other book ids.

    Word order is ignored: a weak reader that copies text between images often reorders it
    ("MATHEMATICS / R.D. SHARMA" for one book, "R.D. SHARMA / MATHEMATICS" for the next)."""
    by_text: dict[str, list[str]] = {}
    for book_id, reading in readings.items():
        key = " ".join(sorted(normalise(reading.text).split()))
        if reading.legible and key:
            by_text.setdefault(key, []).append(book_id)
    return {b: [o for o in ids if o != b] for ids in by_text.values() if len(ids) > 1 for b in ids}


def apply_reading(book: InventoryBook, crop_ref: str, reading: SpineReading | None) -> IdentifiedBook:
    """The record before any catalogue lookup. A spine marked illegible never keeps a title."""
    if reading is None:
        return IdentifiedBook(book_id=book.book_id, frame_ref=book.frame_ref, crop_ref=crop_ref, spine_text="",
                              legible=False, status="unidentified", review=["spine could not be read (no provider answered)"])
    legible = reading.legible and bool(reading.title or reading.text.strip())
    if not legible:
        return IdentifiedBook(book_id=book.book_id, frame_ref=book.frame_ref, crop_ref=crop_ref,
                              spine_text=reading.text, legible=False, status="unidentified",
                              review=["spine not legible"])
    return IdentifiedBook(book_id=book.book_id, frame_ref=book.frame_ref, crop_ref=crop_ref, spine_text=reading.text,
                          legible=True, status="spine_only", title=reading.title or reading.text.split(" / ")[0],
                          author=reading.author, publisher=reading.publisher, source="spine",
                          id_confidence=SPINE_ONLY_CONFIDENCE, review=["no catalogue match: title taken from the spine"])


# ---- Stage ----

@register
class IdentifyStage(Stage):
    name = "identify"
    order = 5
    needs = ("inventory",)
    output = IdentifyOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> IdentifyOutput:
        inventory: InventoryOutput = inputs["inventory"]  # type: ignore[assignment]
        if not inventory.books:
            return IdentifyOutput(reader=None, books=[])
        crops = self._crops(ctx, inventory.books)
        readings, reader = self._read(ctx, inventory.books, crops)

        repeated = repeated_readings(readings)
        if repeated:
            ctx.warn(f"identify: identical spine text read for books {', '.join(sorted(repeated))}; readings not trusted")
        lookup = CatalogueLookup(ctx, ctx.catalogues())
        results = []
        for book in inventory.books:
            record = apply_reading(book, f"debug/spine_{book.book_id}.jpg", readings.get(book.book_id))
            if book.book_id in repeated:
                record = record.model_copy(update={
                    "status": "unidentified", "legible": False, "title": None, "author": None, "publisher": None,
                    "source": None, "id_confidence": 0.0,
                    "review": [f"same spine text as {', '.join(repeated[book.book_id])}: probably a misread (or copies of one book)"]})
            if record.legible:
                lookup.complete(record, readings[book.book_id])
            results.append(record)
        return IdentifyOutput(reader=reader, books=results)

    @staticmethod
    def _crops(ctx: RunContext, books: list[InventoryBook]) -> dict[str, np.ndarray]:
        """Crop of every book from its best view, at full resolution."""
        images: dict[str, np.ndarray] = {}
        crops = {}
        for book in books:
            if book.frame_ref not in images:
                images[book.frame_ref] = read_image(ctx.sweep.frame_path(book.frame_ref))
            crops[book.book_id] = crop(images[book.frame_ref], book.bbox, pad=CROP_PAD)
        return crops

    @staticmethod
    def _read(ctx: RunContext, books: list[InventoryBook], crops: dict[str, np.ndarray]) -> tuple[dict[str, SpineReading], str | None]:
        """Spine readings by book id, in batches; a batch no provider can answer leaves its books unread."""
        try:
            model = ctx.vision("read")
        except ProviderUnavailable as exc:
            ctx.warn(f"identify: no spine reader available ({exc})")
            return {}, None
        prompt = load_prompt("read_spines")
        size = ctx.settings.identify.batch_size
        batches = [books[start:start + size] for start in range(0, len(books), size)]

        def read_batch(batch: list[InventoryBook]) -> ReadAnswer | None:
            """One model call for up to `size` spines; None if no provider could answer it."""
            def accept(answer: ReadAnswer, n=len(batch)) -> str | None:
                got = sorted(s.index for s in answer.spines)
                return None if got == list(range(1, n + 1)) else f"expected one reading per image 1-{n}, got {got}"

            try:
                views = []
                for n, b in enumerate(batch, start=1):
                    view = reading_view(crops[b.book_id], b.orientation, n)
                    write_jpeg(ctx.sweep.debug_dir / f"spine_{b.book_id}.jpg", view)  # exactly what the model sees
                    views.append(encode_jpeg(view, 90, SEND_MAX_SIDE))
                return model.generate_json(prompt, views, ReadAnswer, accept=accept)
            except ProviderUnavailable as exc:
                ctx.warn(f"identify: books {batch[0].book_id}..{batch[-1].book_id} could not be read ({exc})")
                return None

        # Batches are independent, so they run side by side: a slow reader (a CLI model takes about a
        # minute per batch) would otherwise make this stage the longest part of the wait for the packet.
        with ThreadPoolExecutor(IDENTIFY_PARALLEL) as pool:
            answers = list(pool.map(read_batch, batches))
        readings: dict[str, SpineReading] = {}
        for batch, answer in zip(batches, answers, strict=True):
            for spine in answer.spines if answer else []:
                readings[batch[spine.index - 1].book_id] = spine
        return readings, model.name


class CatalogueLookup:
    """Looks legible books up in the catalogues and fills in identification, edition and height.

    A catalogue that fails (network, quota) is warned about once and skipped for the rest of the run.
    """

    def __init__(self, ctx: RunContext, catalogues: list[Catalogue]):
        self.ctx = ctx
        self.catalogues = catalogues
        self.broken: set[str] = set()

    def _call(self, catalogue: Catalogue, fn, *args) -> list[CatalogueRecord]:
        if catalogue.name in self.broken:
            return []
        try:
            return fn(*args)
        except CatalogueError as exc:
            self.broken.add(catalogue.name)
            self.ctx.warn(f"identify: catalogue {catalogue.name} unavailable, skipped from here on ({exc})")
            return []

    def complete(self, record: IdentifiedBook, reading: SpineReading) -> None:
        query = reading.title or reading.text.split(" / ")[0]
        first_author = reading.author.split(",")[0].strip() if reading.author else None

        scored: list[tuple[float, Catalogue, CatalogueRecord]] = []
        for catalogue in self.catalogues:
            candidates = self._call(catalogue, catalogue.search, query, first_author)
            broader = core_title(query)
            if not candidates and broader and broader != query.lower():
                # Catalogues rarely carry "Class XI" in the title: search the core title, score the full one.
                candidates = self._call(catalogue, catalogue.search, broader, first_author)
            for candidate in candidates:
                scored.append((score_candidate(reading, candidate), catalogue, candidate))
        if not scored:
            return
        score, catalogue, best = max(scored, key=lambda s: s[0])
        record.match_score = round(score, 1)
        record.status = status_for(True, score)
        if record.status != "identified":
            return

        record.title = best.title if not best.subtitle else f"{best.title}: {best.subtitle}"
        record.author = ", ".join(best.authors) or reading.author
        record.source, record.source_url = best.source, best.url
        record.id_confidence = round(score / 100, 3)
        record.review = [] if score >= IDENTIFIED else [f"catalogue match only {score:.0f}/100: check the title"]

        # Editions to choose from: a work's editions, or the same catalogue's matching edition-level results.
        if best.edition_level:
            pool = [c for s, cat, c in scored if cat is catalogue and s >= REVIEW]
        else:
            pool = self._call(catalogue, catalogue.editions, best)
        edition = unique_edition(reading.publisher, pool)
        if edition is not None:
            details = self._call(catalogue, catalogue.editions, edition) if edition.source == "googlebooks" else [edition]
            edition = details[0] if details else edition
            record.edition = ", ".join(p for p in (edition.publisher, edition.year) if p)
            record.isbn = edition.isbn
            record.source_url = edition.url
            if edition.height_cm:
                record.catalogue_height_cm, record.height_basis = edition.height_cm, "edition"
                return
        agreed = agreed_height(pool)
        if agreed:
            record.catalogue_height_cm, record.height_basis = agreed[0], f"{agreed[1]} editions agree"
