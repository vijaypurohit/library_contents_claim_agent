"""Currency conversion with daily reference rates (European Central Bank, via the free Frankfurter API).

Rates are cached per day, so a sweep priced twice on one day uses one rate. A price converted from
another currency is always marked `converted` in the price stage's output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from claimagent.providers.base import CatalogueError
from claimagent.providers.http import fetch_json

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

API = "https://api.frankfurter.dev/v1/latest"


class RateUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Rate:
    value: float      # units of the target currency per unit of the source currency
    date: str         # the rate's publication date
    source: str       # URL it came from


def rate(ctx: RunContext, source: str, target: str) -> Rate:
    if source == target:
        return Rate(1.0, datetime.now(UTC).date().isoformat(), "same currency")
    # The day is part of the request, so the cache keeps one answer per day and refreshes the next.
    params = {"from": source, "to": target, "day": datetime.now(UTC).date().isoformat()}
    try:
        data = fetch_json(ctx, API, params)
    except CatalogueError as exc:
        raise RateUnavailable(f"no {source}->{target} rate ({exc})") from exc
    value = (data.get("rates") or {}).get(target)
    if not value:
        raise RateUnavailable(f"no {source}->{target} rate in the answer")
    return Rate(float(value), str(data.get("date")), f"{API}?from={source}&to={target}")
