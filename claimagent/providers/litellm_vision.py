"""Adapter for every provider LiteLLM supports: Gemini, OpenAI, Anthropic, Groq and Ollama.

LiteLLM gives all of them one request format. This adapter adds transport retries on top:
rate limits and server errors are retried, waiting as long as the provider asks; a used-up daily
quota or any other error fails at once, so the next provider in the chain can take over.
Caching, cache-only mode and schema checking come from CachedVisionModel.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import litellm

from claimagent.providers.base import ProviderError, ProviderUnavailable, T
from claimagent.providers.cached import CachedVisionModel, as_int
from claimagent.providers.registry import register_vision

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

litellm.suppress_debug_info = True  # errors are recorded in ModelCall instead of printed

KEYLESS_PROVIDERS = {"ollama"}  # runs locally, no API key
RETRYABLE = (                   # temporary problems: worth waiting and trying again
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.InternalServerError,
    litellm.Timeout,
    litellm.APIConnectionError,
)
# Providers often say how long to wait ("Please retry in 33.0s" / "retryDelay": "33s"); we honour that.
_RETRY_AFTER = re.compile(r"retry in ([\d.]+)\s*s|\"retryDelay\":\s*\"([\d.]+)s\"", re.IGNORECASE)
_MESSAGE = re.compile(r'"message":\s*"([^"]+)')  # the human-readable part of a JSON error body
MAX_WAIT_S = 60.0  # never wait longer than this between attempts


def short_error(exc: Exception) -> str:
    """One readable line instead of the provider's full JSON error body."""
    match = _MESSAGE.search(str(exc))
    text = match.group(1) if match else str(exc)
    return f"{type(exc).__name__}: {text.splitlines()[0][:200]}"


def retry_wait(exc: Exception, attempt: int, backoff_s: float) -> float | None:
    """Seconds to wait before retrying, or None when retrying is pointless (daily quota used up)."""
    text = str(exc)
    if "PerDay" in text:  # e.g. GenerateRequestsPerDayPerProjectPerModel: will not reset for hours
        return None
    match = _RETRY_AFTER.search(text)
    if match:
        return min(float(match.group(1) or match.group(2)) + 1.0, MAX_WAIT_S)
    return min(backoff_s * 2**attempt, MAX_WAIT_S)  # 2 s, 4 s, 8 s, ...


class LiteLLMVision(CachedVisionModel):
    def __init__(self, spec: str, ctx: RunContext, *, sleep: Callable[[float], None] | None = None):
        super().__init__(spec, ctx)
        self.provider = spec.split("/", 1)[0]
        self.api_key = ctx.settings.secrets.key_for(self.provider)
        if self.provider not in KEYLESS_PROVIDERS and not self.api_key:
            raise ProviderUnavailable(f"no API key for {self.provider}")
        self.api_base = ctx.settings.secrets.ollama_api_base if self.provider == "ollama" else None
        self._sleep = sleep or time.sleep  # tests pass a no-op so retries don't slow them down

    def _complete(self, prompt: str, images: Sequence[bytes], schema: type[T]) -> str:
        cfg = self.ctx.settings.gateway
        # One user message: the prompt text, then each image as an inline JPEG.
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
            for img in images
        ]
        last_error = "no attempt made"
        for attempt in range(cfg.retries + 1):
            start = time.perf_counter()
            try:
                response = litellm.completion(
                    model=self.name,
                    messages=[{"role": "user", "content": content}],
                    api_key=self.api_key,
                    api_base=self.api_base,
                    response_format={"type": "json_object"},
                    timeout=cfg.timeout_s,
                )
            except RETRYABLE as exc:
                last_error = short_error(exc)
                self.record(time.perf_counter() - start, ok=False, error=last_error)
                wait = retry_wait(exc, attempt, cfg.backoff_s)
                if wait is None:
                    raise ProviderUnavailable(f"quota used up ({last_error})") from exc
                if attempt < cfg.retries:
                    self._sleep(wait)
                continue
            except Exception as exc:  # bad key, bad request, unknown model: retrying will not help
                self.record(time.perf_counter() - start, ok=False, error=short_error(exc))
                raise ProviderError(short_error(exc)) from exc

            usage = getattr(response, "usage", None)
            try:
                cost = litellm.completion_cost(completion_response=response)
            except Exception:  # model missing from LiteLLM's price list: cost unknown, not zero
                cost = None
            self.record(time.perf_counter() - start, cost=cost,
                        input_tokens=as_int(getattr(usage, "prompt_tokens", None)),
                        output_tokens=as_int(getattr(usage, "completion_tokens", None)))
            return response.choices[0].message.content or ""
        raise ProviderUnavailable(f"gave up after {cfg.retries + 1} attempts ({last_error})")


@register_vision("gemini", "openai", "anthropic", "groq", "ollama")
def _factory(spec: str, ctx: RunContext) -> LiteLLMVision:
    return LiteLLMVision(spec, ctx)
