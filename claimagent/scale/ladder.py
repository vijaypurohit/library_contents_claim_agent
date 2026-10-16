"""The scale ladder: choose one scale per plane from whatever sources produced an estimate.

Sources are ranked (card, then catalogue heights, then known objects, then depth). The best-ranked
estimate wins; among estimates of the same rank, the most confident. Every other estimate is then
used as a cross-check:
- one that agrees within `agree_within` raises the confidence a little
- one that disagrees by more than that lowers it, and the disagreement is recorded for review
The chosen estimate keeps its own `method`, so the packet always says where a size came from.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from claimagent.scale.sources import Method, ScaleEstimate

RANK: dict[Method, int] = {"card": 0, "catalogue_height": 1, "known_object": 2, "depth": 3}
AGREE_BONUS = 0.05
CONFLICT_FACTOR = 0.7


class LadderChoice(BaseModel):
    chosen: ScaleEstimate | None                   # None: no source could size this plane
    agreed_with: list[Method] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)  # human-readable, for the review queue


def choose(estimates: list[ScaleEstimate], agree_within: float = 0.10) -> LadderChoice:
    """Pick the best estimate and adjust its confidence by how well the others agree with it."""
    if not estimates:
        return LadderChoice(chosen=None)
    ordered = sorted(estimates, key=lambda e: (RANK[e.method], -e.confidence))
    best, others = ordered[0], ordered[1:]

    agreed: list[Method] = []
    conflicts: list[str] = []
    confidence = best.confidence
    for other in others:
        ratio = other.cm_per_px / best.cm_per_px
        if abs(ratio - 1) <= agree_within:
            agreed.append(other.method)
            confidence = min(1.0, confidence + AGREE_BONUS)
        else:
            conflicts.append(f"{other.method} scale differs from {best.method} by {100 * (ratio - 1):+.0f}%")
            confidence *= CONFLICT_FACTOR
    return LadderChoice(chosen=best.model_copy(update={"confidence": round(confidence, 3)}),
                        agreed_with=agreed, conflicts=conflicts)
