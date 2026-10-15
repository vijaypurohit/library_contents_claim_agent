"""Small data records shared by several stages.

Stage-specific outputs (e.g. what detect returns) are defined next to their stage instead.
All are Pydantic models, so they validate on creation and save to JSON as-is.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FrameRef(BaseModel):
    """A saved frame. `name` is the file name inside the sweep's frames/ folder."""

    model_config = ConfigDict(frozen=True)

    name: str
    index: int
    t: float


class FrameInfo(FrameRef):
    """A frame as recorded in the manifest."""

    source: str  # file the frame came from
    width: int
    height: int
    hfov_deg: float | None = None  # horizontal field of view; None when the source had no focal length


class Manifest(BaseModel):
    """What ingest produced: the sources, the sampling rate and every frame."""

    sweep_id: str
    created_at: datetime
    sources: list[str]
    fps: float
    frames: list[FrameInfo]


class BBox(BaseModel):
    """Axis-aligned box in pixel coordinates of a specific frame."""

    model_config = ConfigDict(frozen=True)

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(self.width, 0.0) * max(self.height, 0.0)


class Detection(BaseModel):
    """One box from a detector model, before any stage-specific interpretation."""

    label: str
    bbox: BBox
    score: float | None = None  # detector confidence, when the model reports one


class ModelCall(BaseModel):
    """One model request, recorded for cost and latency reporting."""

    stage: str
    provider: str            # provider spec, e.g. "gemini/gemini-3.5-flash"
    latency_s: float
    cached: bool = False     # answered from the cache: no cost, no network
    ok: bool = True          # False for failed attempts (they are kept to show retries)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None  # None when the provider does not publish a price for the model
    error: str | None = None
