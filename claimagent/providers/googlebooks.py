"""Catalogue adapter for Google Books (googleapis.com/books).

Every search result is one edition (a "volume") with its publisher and ISBN. The printed height is
only in a volume's full record, so `editions` fetches that. Without GOOGLE_BOOKS_API_KEY the API
runs on a small shared anonymous quota that is often used up; the stage then warns and carries on
with the other catalogues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claimagent.domain.records import CatalogueRecord
from claimagent.providers.http import fetch_json
from claimagent.text.units import book_height_cm

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

BASE = "https://www.googleapis.com/books/v1/volumes"
SEARCH_LIMIT = 10


class GoogleBooks:
    name = "googlebooks"

    def __init__(self, ctx: RunContext):
        self.ctx = ctx
        key = ctx.settings.secrets.google_books_api_key
        self._secret = {"key": key} if key else None

    def search(self, title: str, author: str | None) -> list[CatalogueRecord]:
        query = f'intitle:"{title}"' + (f" inauthor:{author.split(',')[0].split()[-1]}" if author and author.split() else "")
        params = {"q": query, "maxResults": SEARCH_LIMIT, "country": self.ctx.settings.locale.country,
                  "printType": "books"}
        data = fetch_json(self.ctx, BASE, params, secret_params=self._secret)
        return [parse_volume(item) for item in data.get("items", []) if item.get("volumeInfo", {}).get("title")]

    def editions(self, record: CatalogueRecord) -> list[CatalogueRecord]:
        data = fetch_json(self.ctx, f"{BASE}/{record.id}", secret_params=self._secret)
        return [parse_volume(data)]


def parse_volume(item: dict) -> CatalogueRecord:
    info = item["volumeInfo"]
    identifiers = {i.get("type"): i.get("identifier") for i in info.get("industryIdentifiers", [])}
    return CatalogueRecord(source="googlebooks", id=item["id"],
                           url=info.get("infoLink") or f"https://books.google.com/books?id={item['id']}",
                           title=info["title"], subtitle=info.get("subtitle"), authors=info.get("authors") or [],
                           publisher=info.get("publisher"), year=info.get("publishedDate"),
                           isbn=identifiers.get("ISBN_13") or identifiers.get("ISBN_10"), edition_level=True,
                           height_cm=book_height_cm((info.get("dimensions") or {}).get("height")))
