"""Identify stage: spine readings, catalogue matching, edition and height rules. No network, no models."""

import httpx
import numpy as np
import pytest

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.stage import Stage
from claimagent.domain.records import BBox, CatalogueRecord
from claimagent.providers.base import CatalogueError
from claimagent.providers.googlebooks import parse_volume
from claimagent.providers.http import fetch_json
from claimagent.providers.openlibrary import parse_edition, parse_work
from claimagent.providers.registry import FallbackVision
from claimagent.stages.identify import (IdentifyStage, repeated_readings, ReadAnswer, SpineReading, agreed_height, apply_reading,
                                        reading_view, status_for, unique_edition)
from claimagent.stages.inventory import InventoryBook, InventoryOutput, View


def book(book_id: str, frame: str, orientation="upright") -> InventoryBook:
    bbox = BBox(x0=10, y0=4, x1=30, y1=44) if orientation == "upright" else BBox(x0=4, y0=10, x1=60, y1=24)
    return InventoryBook(book_id=book_id, unit="A", shelf=1, position=int(book_id[-1]), orientation=orientation,
                         frame_ref=frame, bbox=bbox, plane_box=bbox, views=[View(frame=frame, bbox=bbox, truncated=False)],
                         annotated_ref=None, review=[])


def edition(publisher, height=None, isbn="9780000000001", year="2019") -> CatalogueRecord:
    return CatalogueRecord(source="openlibrary", id=f"/books/{publisher}", url="https://openlibrary.org/x", title="Word Power Made Easy",
                           authors=["Norman Lewis"], publisher=publisher, year=year, isbn=isbn, edition_level=True,
                           height_cm=height)


WORK = CatalogueRecord(source="openlibrary", id="/works/W1", url="https://openlibrary.org/works/W1",
                       title="Word Power Made Easy", authors=["Norman Lewis"])


class FakeReader:
    def __init__(self, answers):
        self.name, self.answers, self.calls = "fake-reader", list(answers), 0

    def generate_json(self, prompt, images, schema):
        self.calls += 1
        return self.answers.pop(0)


class FakeCatalogue:
    name = "fake"

    def __init__(self, results=None, editions=None, error=None):
        self.results, self._editions, self.error, self.searches = results or {}, editions or [], error, []

    def search(self, title, author):
        self.searches.append((title, author))
        if self.error:
            raise CatalogueError(self.error)
        return self.results.get(title.lower(), [])

    def editions(self, record):
        return self._editions


def run_identify(ctx: RunContext, monkeypatch, books, answers, catalogue, batch_size=8):
    ctx.settings.identify.batch_size = batch_size
    reader = FakeReader(answers)
    monkeypatch.setattr(ctx, "vision", lambda stage: FallbackVision([reader], ctx))
    monkeypatch.setattr(ctx, "catalogues", lambda: [catalogue])

    class FakeInventory(Stage):
        name, order, needs, output = "inventory", 4, (), InventoryOutput

        def run(self, ctx, inputs):
            return InventoryOutput(reference_frame=books[0].frame_ref, books=books, items=[], shelves=[],
                                   unconfirmed=[], unlinked_frames=[])

    runner = Runner(ctx, {"inventory": FakeInventory, "identify": IdentifyStage})
    runner.run_stage("inventory")
    return runner.run_stage("identify"), reader


# ---- Pure rules ----

@pytest.mark.parametrize("legible,score,status", [
    (True, 95, "identified"), (True, 80, "identified"), (True, 60, "spine_only"), (True, None, "spine_only"),
    (False, 99, "unidentified"),
])
def test_status_thresholds(legible, score, status):
    assert status_for(legible, score) == status


def test_illegible_spine_never_keeps_a_title_even_if_the_model_gives_one():
    reading = SpineReading(index=1, legible=False, text="MATH", title="Mathematics", author="R.D. Sharma")
    record = apply_reading(book("A1-1", "f.jpg"), "debug/x.jpg", reading)
    assert record.status == "unidentified" and record.title is None and record.author is None
    assert record.spine_text == "MATH"


def test_devanagari_title_passes_through_unchanged():
    record = apply_reading(book("A1-1", "f.jpg"), "debug/x.jpg", SpineReading(index=1, legible=True, text="सामान्य हिन्दी"))
    assert record.title == "सामान्य हिन्दी" and record.status == "spine_only"


def test_edition_only_on_a_unique_publisher_match():
    editions = [edition("Penguin India"), edition("Goyal Publishers"), edition("Goyal Publishers", year="2021")]
    assert unique_edition("Penguin", editions).publisher == "Penguin India"
    assert unique_edition("Goyal", editions) is None          # two Goyal editions: can't tell which
    assert unique_edition(None, editions) is None


def test_height_from_editions_only_when_they_agree():
    assert agreed_height([edition("a", 21.0), edition("b", 21.6), edition("c")]) == (21.3, 2)
    assert agreed_height([edition("a", 17.3), edition("b", 21.0)]) is None   # pocket vs trade size
    assert agreed_height([edition("a")]) is None


def test_reading_view_shows_every_orientation():
    patch = np.zeros((200, 40, 3), np.uint8)
    view = reading_view(patch, "upright", 3)
    assert view.shape[1] >= 40 + 200          # original beside the turned copies
    flat = reading_view(np.zeros((40, 200, 3), np.uint8), "flat", 1)
    assert flat.shape[1] == 200 and flat.shape[0] > 80


# ---- Stage ----

def test_stage_identifies_with_unique_edition_and_its_height(ctx: RunContext, monkeypatch):
    frame = ctx.sweep.frames()[0].name
    answer = ReadAnswer(spines=[SpineReading(index=1, legible=True, text="Word Power Made Easy / Norman Lewis / Penguin",
                                             title="Word Power Made Easy", author="Norman Lewis", publisher="Penguin")])
    catalogue = FakeCatalogue({"word power made easy": [WORK]},
                              editions=[edition("Penguin India", height=19.8), edition("Goyal Publishers", height=21.5)])
    out, reader = run_identify(ctx, monkeypatch, [book("A1-1", frame)], [answer], catalogue)
    record = out.data["books"][0]
    assert record["status"] == "identified" and record["match_score"] == 100
    assert record["edition"] == "Penguin India, 2019" and record["isbn"] == "9780000000001"
    assert record["catalogue_height_cm"] == 19.8 and record["height_basis"] == "edition"
    assert (ctx.sweep.debug_dir / "spine_A1-1.jpg").exists()


def test_stage_keeps_spine_text_when_no_catalogue_knows_the_book(ctx: RunContext, monkeypatch):
    frame = ctx.sweep.frames()[0].name
    answer = ReadAnswer(spines=[SpineReading(index=1, legible=True, text="SANJIV ENGLISH COURSE / Class 10",
                                             title="SANJIV ENGLISH COURSE")])
    catalogue = FakeCatalogue()
    out, _ = run_identify(ctx, monkeypatch, [book("A2-2", frame)], [answer], catalogue)
    record = out.data["books"][0]
    assert record["status"] == "spine_only" and record["title"] == "SANJIV ENGLISH COURSE"
    assert record["isbn"] is None and record["review"]


def test_empty_search_is_retried_without_class_labels(ctx: RunContext, monkeypatch):
    frame = ctx.sweep.frames()[0].name
    answer = ReadAnswer(spines=[SpineReading(index=1, legible=True, text="R.D. SHARMA / MATHEMATICS / CLASS XI",
                                             title="MATHEMATICS CLASS XI", author="R.D. SHARMA")])
    catalogue = FakeCatalogue()
    run_identify(ctx, monkeypatch, [book("A1-2", frame)], [answer], catalogue)
    assert catalogue.searches == [("MATHEMATICS CLASS XI", "R.D. SHARMA"), ("mathematics", "R.D. SHARMA")]


def test_stage_batches_and_a_failing_catalogue_is_warned_once(ctx: RunContext, monkeypatch):
    frame = ctx.sweep.frames()[0].name
    books = [book(f"A1-{i}", frame) for i in (1, 2, 3)]
    first = ReadAnswer(spines=[SpineReading(index=i, legible=True, title=f"Book {i}", text=f"Book {i}") for i in (1, 2)])
    second = ReadAnswer(spines=[SpineReading(index=1, legible=True, title="Book 3", text="Book 3")])
    out, reader = run_identify(ctx, monkeypatch, books, [first, second], FakeCatalogue(error="HTTP 429"), batch_size=2)
    assert reader.calls == 2
    assert [b["title"] for b in out.data["books"]] == ["Book 1", "Book 2", "Book 3"]
    assert sum("catalogue fake unavailable" in w for w in out.warnings) == 1


def test_wrong_number_of_readings_is_rejected(ctx: RunContext, monkeypatch):
    frame = ctx.sweep.frames()[0].name
    short = ReadAnswer(spines=[SpineReading(index=1, legible=True, title="Only one", text="Only one")])
    out, _ = run_identify(ctx, monkeypatch, [book("A1-1", frame), book("A1-2", frame)], [short], FakeCatalogue())
    assert all(b["status"] == "unidentified" for b in out.data["books"])
    assert any("could not be read" in w for w in out.warnings)


# ---- Catalogue adapters and the cached fetch ----

def test_open_library_parsing():
    work = parse_work({"key": "/works/OL1W", "title": "Word Power Made Easy", "author_name": ["Norman Lewis"],
                       "first_publish_year": 1949})
    assert work.authors == ["Norman Lewis"] and not work.edition_level and work.year == "1949"
    ed = parse_edition({"key": "/books/OL2M", "publishers": ["Pocket"], "publish_date": "1991",
                        "isbn_13": ["9780671741907"], "physical_dimensions": "6.8 x 3.9 x 1.3 inches"}, work)
    assert ed.edition_level and ed.publisher == "Pocket" and ed.isbn == "9780671741907" and ed.height_cm == 17.3


def test_google_books_parsing():
    record = parse_volume({"id": "abc", "volumeInfo": {
        "title": "Word Power Made Easy", "authors": ["Norman Lewis"], "publisher": "Penguin UK",
        "industryIdentifiers": [{"type": "ISBN_10", "identifier": "0143424688"}, {"type": "ISBN_13", "identifier": "9780143424680"}],
        "dimensions": {"height": "19.80 cm"}, "infoLink": "https://books.google.com/x"}})
    assert record.isbn == "9780143424680" and record.height_cm == 19.8 and record.edition_level


def test_fetch_json_caches_and_keeps_keys_out_of_the_cache(ctx: RunContext, monkeypatch):
    calls = []

    def fake_get(url, params, headers, timeout, follow_redirects):
        calls.append(params)
        return httpx.Response(200, text='{"ok": 1}')

    monkeypatch.setattr(httpx, "get", fake_get)
    assert fetch_json(ctx, "https://api.example.org/v", {"q": "x"}, secret_params={"key": "SECRET"}) == {"ok": 1}
    assert fetch_json(ctx, "https://api.example.org/v", {"q": "x"}, secret_params={"key": "OTHER"}) == {"ok": 1}
    assert len(calls) == 1 and calls[0]["key"] == "SECRET"   # second answer came from the cache


def test_fetch_json_failure_is_not_cached(ctx: RunContext, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(429, text="quota"))
    with pytest.raises(CatalogueError):
        fetch_json(ctx, "https://api.example.org/v", {"q": "y"})
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, text="[]"))
    assert fetch_json(ctx, "https://api.example.org/v", {"q": "y"}) == []


def test_identical_text_for_different_books_is_not_trusted(ctx: RunContext, monkeypatch):
    frame = ctx.sweep.frames()[0].name
    copy = dict(legible=True, text="SANJIV SINHA / INORGANIC CHEMISTRY", title="INORGANIC CHEMISTRY")
    answer = ReadAnswer(spines=[SpineReading(index=1, **copy), SpineReading(index=2, **copy),
                                SpineReading(index=3, legible=True, text="Word Power Made Easy", title="Word Power Made Easy")])
    books = [book(f"A2-{i}", frame) for i in (1, 2, 3)]
    out, _ = run_identify(ctx, monkeypatch, books, [answer], FakeCatalogue())
    statuses = [b["status"] for b in out.data["books"]]
    assert statuses == ["unidentified", "unidentified", "spine_only"]
    assert out.data["books"][0]["title"] is None and "A2-2" in out.data["books"][0]["review"][0]


def test_reordered_copies_of_one_text_are_distrusted():
    """A weak reader that copies text between images often reorders it; word order must not hide that."""
    read = lambda text: SpineReading(index=1, legible=True, text=text)
    readings = {"A1-2": read("MATHEMATICS / R.D. SHARMA"), "A1-3": read("R.D. SHARMA / MATHEMATICS"),
                "A1-4": read("Word Power Made Easy / Norman Lewis")}
    assert repeated_readings(readings) == {"A1-2": ["A1-3"], "A1-3": ["A1-2"]}
