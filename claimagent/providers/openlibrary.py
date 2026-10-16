"""Catalogue adapter for Open Library (openlibrary.org): free, no key.

Search returns works (a book in all its editions). `editions` then lists a work's editions, each
with its publisher, ISBN and, for some, physical dimensions, which give the printed height.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claimagent.domain.records import CatalogueRecord
from claimagent.providers.http import fetch_json
from claimagent.text.units import book_height_cm

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

BASE = "https://openlibrary.org"
SEARCH_LIMIT = 8
EDITIONS_LIMIT = 100


class OpenLibrary:
    name = "openlibrary"

    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    def search(self, title: str, author: str | None) -> list[CatalogueRecord]:
        params = {"title": title, "limit": SEARCH_LIMIT, "fields": "key,title,subtitle,author_name,first_publish_year"}
        if author:
            params["author"] = author
        data = fetch_json(self.ctx, f"{BASE}/search.json", params)
        return [parse_work(doc) for doc in data.get("docs", []) if doc.get("title")]

    def editions(self, record: CatalogueRecord) -> list[CatalogueRecord]:
        if record.edition_level:
            return [record]
        data = fetch_json(self.ctx, f"{BASE}{record.id}/editions.json", {"limit": EDITIONS_LIMIT})
        return [parse_edition(entry, record) for entry in data.get("entries", []) if entry.get("key")]


def parse_work(doc: dict) -> CatalogueRecord:
    return CatalogueRecord(source="openlibrary", id=doc["key"], url=f"{BASE}{doc['key']}", title=doc["title"],
                           subtitle=doc.get("subtitle"), authors=doc.get("author_name") or [],
                           year=str(doc["first_publish_year"]) if doc.get("first_publish_year") else None)


def parse_edition(entry: dict, work: CatalogueRecord) -> CatalogueRecord:
    isbns = (entry.get("isbn_13") or []) + (entry.get("isbn_10") or [])
    publishers = entry.get("publishers") or []
    return CatalogueRecord(source="openlibrary", id=entry["key"], url=f"{BASE}{entry['key']}",
                           title=entry.get("title") or work.title, subtitle=entry.get("subtitle"),
                           authors=work.authors, publisher=publishers[0] if publishers else None,
                           year=entry.get("publish_date"), isbn=isbns[0] if isbns else None, edition_level=True,
                           height_cm=book_height_cm(entry.get("physical_dimensions")))
