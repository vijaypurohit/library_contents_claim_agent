"""Provider registry and fallback chains.

A provider spec is "<prefix>/<model>", e.g. "gemini/gemini-3.5-flash" or "ollama/qwen2.5vl:7b".
The prefix picks the adapter class; the settings give each model stage an ordered list of specs.

Adding a provider means writing one adapter module and decorating its factory with
@register_vision("<prefix>"). No stage code changes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from claimagent.providers.base import Catalogue, ProviderError, ProviderUnavailable, T, VisionModel

if TYPE_CHECKING:
    from claimagent.core.context import RunContext

VisionFactory = Callable[[str, "RunContext"], VisionModel]
_VISION: dict[str, VisionFactory] = {}  # prefix -> function that builds the adapter


def register_vision(*prefixes: str) -> Callable[[VisionFactory], VisionFactory]:
    def decorator(factory: VisionFactory) -> VisionFactory:
        for prefix in prefixes:
            _VISION[prefix] = factory
        return factory

    return decorator


def create_vision(spec: str, ctx: RunContext) -> VisionModel:
    """Build the adapter for one spec. Raises ProviderUnavailable if it cannot be used (e.g. no key)."""
    import claimagent.providers.claude_cli  # noqa: F401  registers "claude-cli"
    import claimagent.providers.litellm_vision  # noqa: F401  registers the hosted and Ollama prefixes

    prefix = spec.split("/", 1)[0]
    if prefix not in _VISION:
        raise ProviderUnavailable(f"no provider registered for {spec!r}")
    return _VISION[prefix](spec, ctx)


class FallbackVision:
    """Behaves like one VisionModel but tries several in order.

    When one provider fails (overloaded, quota used up, bad output), the next is tried and a
    warning is recorded, so the stage output shows which provider actually answered.
    """

    def __init__(self, models: Sequence[VisionModel], ctx: RunContext):
        if not models:
            raise ProviderUnavailable("no usable provider")
        self.models = list(models)
        self.ctx = ctx
        self.name = " -> ".join(m.name for m in self.models)

    def generate_json(self, prompt: str, images: Sequence[bytes], schema: type[T],
                      accept: Callable[[T], str | None] | None = None) -> T:
        """Ask each model in turn until one answers.

        `accept` lets the caller reject an answer that is valid JSON but makes no sense (for example,
        mostly impossible boxes). It returns None to accept, or the reason for rejecting it.
        """
        errors = []
        for model in self.models:
            try:
                result = model.generate_json(prompt, images, schema)
            except ProviderError as exc:
                errors.append(f"{model.name}: {exc}")
                self.ctx.warn(f"{self.ctx.stage}: {model.name} failed ({exc}); trying next provider")
                continue
            reason = accept(result) if accept else None
            if reason is None:
                return result
            errors.append(f"{model.name}: rejected ({reason})")
            self.ctx.warn(f"{self.ctx.stage}: {model.name} answer rejected ({reason}); trying next provider")
        raise ProviderUnavailable("all providers failed: " + "; ".join(errors))


def build_vision(ctx: RunContext, model_stage: str) -> FallbackVision:
    """The configured chain for a model stage, skipping providers that cannot be used (with a warning)."""
    specs = ctx.settings.models.chain(model_stage)
    if not specs:
        raise ProviderUnavailable(f"no providers configured for model stage {model_stage!r}")
    models = []
    for spec in specs:
        try:
            models.append(create_vision(spec, ctx))
        except ProviderUnavailable as exc:
            ctx.warn(f"{model_stage}: skipping {spec} ({exc})")
    return FallbackVision(models, ctx)


def build_catalogues(ctx: RunContext) -> list[Catalogue]:
    """The configured book catalogues, in order. Unknown names are skipped with a warning."""
    from claimagent.providers.googlebooks import GoogleBooks
    from claimagent.providers.openlibrary import OpenLibrary

    known: dict[str, Callable[[RunContext], Catalogue]] = {"openlibrary": OpenLibrary, "googlebooks": GoogleBooks}
    catalogues = []
    for name in ctx.settings.identify.catalogues:
        if name in known:
            catalogues.append(known[name](ctx))
        else:
            ctx.warn(f"unknown catalogue {name!r}; known: {sorted(known)}")
    return catalogues
