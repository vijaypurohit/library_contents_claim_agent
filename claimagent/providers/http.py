"""Cached JSON fetching for web APIs (book catalogues now, other public APIs later).

Every successful answer is stored in the shared DiskCache under the "web" namespace, keyed by the
full URL, so a rerun or a test replays it without the network. Failures are never cached.
Catalogue lookups are free and need no key, so unlike model calls they also run in cache-only mode.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from claimagent.providers.base import CatalogueError

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

USER_AGENT = "claimagent/0.1 (home library inventory)"  # Open Library asks clients to identify themselves
TIMEOUT_S = 15.0


def fetch_json(ctx: RunContext, url: str, params: dict[str, Any] | None = None, *,
               secret_params: dict[str, str] | None = None) -> Any:
    """GET a JSON document, from the cache when possible.

    `secret_params` (API keys) are sent but left out of the cache key, so a key never lands on disk
    and changing it does not invalidate earlier answers.
    """
    request = httpx.Request("GET", url, params=params or {})
    key = ctx.cache.key("GET", str(request.url))
    cached = ctx.cache.get("web", key)
    if cached is not None:
        return json.loads(cached)
    try:
        response = httpx.get(url, params={**(params or {}), **(secret_params or {})},
                             headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise CatalogueError(f"{request.url.host}: {type(exc).__name__}") from exc
    if response.status_code != 200:
        message = response.text[:160].replace("\n", " ")
        raise CatalogueError(f"{request.url.host}: HTTP {response.status_code} {message}")
    ctx.cache.set("web", key, response.text)
    return response.json()
