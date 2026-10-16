"""CachedVisionModel: the behaviour every model adapter shares, written once.

An adapter (LiteLLM, Claude CLI, ...) only implements `_complete`: send a prompt and images,
return the raw text answer. This base class adds:
- the JSON-schema instruction appended to every prompt
- the cache: an identical request is answered from disk, for free
- cache-only mode: never call a model, replay cached answers only (development and tests). If this
  provider has no answer, an answer another provider gave to the identical prompt and images is
  replayed instead (and recorded under that provider), so a replay works whatever the profile
- checking that the answer matches the schema, asking again if it does not
- recording every call for the stage's cost and latency report
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from claimagent.domain.records import ModelCall
from claimagent.providers.base import ProviderUnavailable, T

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)  # some models wrap JSON in ``` fences
SCHEMA_ATTEMPTS = 2  # ask again once when an answer does not match the schema


def schema_instruction(schema: type[T]) -> str:
    """Appended to every prompt so any provider knows the exact JSON shape expected."""
    return (
        "\n\nRespond with a single JSON object only, no prose, matching this JSON Schema:\n"
        + json.dumps(schema.model_json_schema(), separators=(",", ":"))
    )


def parse_answer(text: str, schema: type[T]) -> T:
    return schema.model_validate_json(_FENCE.sub("", text.strip()))


class CachedVisionModel(ABC):
    def __init__(self, spec: str, ctx: RunContext):
        self.name = spec
        self.ctx = ctx

    @abstractmethod
    def _complete(self, prompt: str, images: Sequence[bytes], schema: type[T]) -> str:
        """Send one request and return the raw text answer. Handle transport retries; record attempts."""

    def generate_json(self, prompt: str, images: Sequence[bytes], schema: type[T]) -> T:
        full_prompt = prompt + schema_instruction(schema)
        cache_key = self.ctx.cache.key(self.name, full_prompt, *images)

        any_key = self.ctx.cache.key(full_prompt, *images)   # the same request, whichever provider answered
        cached = self.ctx.cache.get("llm", cache_key)
        if cached is not None:
            self.record(0.0, cached=True)
            self._remember_for_any(any_key, cached)
            return parse_answer(cached, schema)
        if self.ctx.settings.gateway.cache_only:
            other = self.ctx.cache.get("llm-any", any_key)
            if other is not None:
                entry = json.loads(other)
                self.record(0.0, cached=True, provider=entry["provider"])
                return parse_answer(entry["text"], schema)
            raise ProviderUnavailable("no cached answer and cache-only mode is on (use --allow-calls to call models)")

        last_error = ""
        for _ in range(SCHEMA_ATTEMPTS):
            text = self._complete(full_prompt, images, schema)
            try:
                result = parse_answer(text, schema)
            except ValidationError as exc:  # answered, but not in the shape we asked for: ask again
                last_error = f"invalid JSON for {schema.__name__}: {exc.errors()[:2]}"
                self.record(0.0, ok=False, error=last_error)
                continue
            self.ctx.cache.set("llm", cache_key, text)  # only valid answers are cached
            self._remember_for_any(any_key, text)
            return result
        raise ProviderUnavailable(last_error)

    def _remember_for_any(self, any_key: str, text: str) -> None:
        """Index an answer by the request alone, for provider-independent replay."""
        if self.ctx.cache.get("llm-any", any_key) is None:
            self.ctx.cache.set("llm-any", any_key, json.dumps({"provider": self.name, "text": text}))

    def record(self, latency: float, *, cached: bool = False, ok: bool = True, error: str | None = None,
               input_tokens: int | None = None, output_tokens: int | None = None, cost: float | None = None,
               provider: str | None = None) -> None:
        self.ctx.record_call(ModelCall(
            stage=self.ctx.stage, provider=provider or self.name, latency_s=round(latency, 3), cached=cached, ok=ok,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost, error=error,
        ))


def as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
