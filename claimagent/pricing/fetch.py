"""Fetching shop pages: cached, and saved as evidence.

Every page a price is read from is kept twice:
- in the shared DiskCache ("web" namespace), so reruns and tests replay it without the network
- as a snapshot file under the sweep's evidence/prices/, which the claim packet links to, so a
  person can see the page as it was when the price was read

Pages are fetched with plain HTTP. Some shops refuse that (Amazon answers 503, others 403/406);
those raise ShopBlocked and the price stage lists them in its warnings. A headless-browser
fetcher can be added behind the same function for such shops.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

# A desktop browser's user agent: several shops return an empty page or an error to unknown clients.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TIMEOUT_S = 20.0


class ShopBlocked(RuntimeError):
    """The shop refused the request or could not be reached."""


@dataclass(frozen=True)
class Page:
    url: str
    text: str
    retrieved_at: datetime
    snapshot_ref: str          # path relative to the sweep folder, e.g. "evidence/prices/3f2a....html"


def fetch_page(ctx: RunContext, url: str) -> Page:
    """The page at `url`, from the cache when possible, with its snapshot written to the sweep."""
    key = ctx.cache.key("GET", url)
    cached = ctx.cache.get("web-page", key)
    if cached is not None:
        entry = json.loads(cached)
        text, retrieved_at = entry["text"], datetime.fromisoformat(entry["retrieved_at"])
    else:
        try:
            response = httpx.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"},
                                 timeout=TIMEOUT_S, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ShopBlocked(f"{httpx.URL(url).host}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ShopBlocked(f"{httpx.URL(url).host}: HTTP {response.status_code}")
        text, retrieved_at = response.text, datetime.now(UTC)
        ctx.cache.set("web-page", key, json.dumps({"text": text, "retrieved_at": retrieved_at.isoformat()}))
    return Page(url=url, text=text, retrieved_at=retrieved_at, snapshot_ref=_snapshot(ctx, url, text))


def _snapshot(ctx: RunContext, url: str, text: str) -> str:
    """Write the page under evidence/prices/, named by a hash of its URL; JSON answers keep a .json name."""
    folder = ctx.sweep.evidence_dir / "prices"
    folder.mkdir(exist_ok=True)
    suffix = ".json" if text.lstrip()[:1] in "{[" else ".html"
    name = hashlib.sha256(url.encode()).hexdigest()[:20] + suffix
    path = folder / name
    if not path.exists():
        path.write_text(text)
    return f"evidence/prices/{name}"
