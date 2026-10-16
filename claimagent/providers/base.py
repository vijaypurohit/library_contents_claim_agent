"""Interfaces that stages depend on, instead of depending on a specific provider.

Each interface is deliberately small, so a local detector does not have to pretend to be a chat model:
- VisionModel:    images + prompt -> structured JSON (Gemini, GPT, Claude, Qwen via Ollama, ...)
- Detector:       image -> boxes for given labels (e.g. OWLv2, running locally)
- DepthEstimator: image -> metric depth map (e.g. Depth Anything, running locally)
- Catalogue:      title + author -> candidate book records (Open Library, Google Books)
Concrete adapters live in this package and are created through registry.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar, runtime_checkable

import numpy as np
from pydantic import BaseModel

from claimagent.domain.records import CatalogueRecord, Detection

T = TypeVar("T", bound=BaseModel)  # the Pydantic model a caller expects back


class ProviderError(RuntimeError):
    """A provider failed in a way that the next provider in the chain may not."""


class ProviderUnavailable(ProviderError):
    """The provider cannot be used at all: missing key, or retries exhausted."""


class CatalogueError(RuntimeError):
    """A catalogue lookup failed (network, quota). The stage carries on with the other catalogues."""


@runtime_checkable
class VisionModel(Protocol):
    name: str

    def generate_json(self, prompt: str, images: Sequence[bytes], schema: type[T]) -> T:
        """Answer `prompt` about the JPEG `images`, returning an instance of `schema`."""
        ...


@runtime_checkable
class Detector(Protocol):
    name: str

    def detect(self, image: np.ndarray, labels: Sequence[str]) -> list[Detection]:
        """Boxes for every instance of `labels` in a BGR image."""
        ...


@runtime_checkable
class DepthEstimator(Protocol):
    name: str

    def depth_m(self, image: np.ndarray, hfov_deg: float) -> np.ndarray:
        """Per-pixel metric depth in metres for a BGR image."""
        ...


@runtime_checkable
class Catalogue(Protocol):
    name: str

    def search(self, title: str, author: str | None) -> list[CatalogueRecord]:
        """Candidate records for a title (and author, when known), best guesses first."""
        ...

    def editions(self, record: CatalogueRecord) -> list[CatalogueRecord]:
        """Edition-level records of a search result: publisher, ISBN and printed height where known.
        An edition-level record returns itself, completed with details."""
        ...
