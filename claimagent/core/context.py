"""RunContext: everything a stage may use, built once and handed to it.

Stages never create settings, caches or model clients themselves; they ask the context.
Tests build a context with fakes the same way, so stages run without network or keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from claimagent.core.cache import DiskCache
from claimagent.core.settings import Settings
from claimagent.core.sweep import SweepDir
from claimagent.domain.records import ModelCall

if TYPE_CHECKING:
    from claimagent.providers.base import Catalogue
    from claimagent.providers.registry import FallbackVision


@dataclass
class CallTracker:
    """Model calls and warnings of the stage that is running. The Runner empties it before each stage."""

    calls: list[ModelCall] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.calls.clear()
        self.warnings.clear()


@dataclass
class RunContext:
    settings: Settings
    sweep: SweepDir
    cache: DiskCache
    tracker: CallTracker = field(default_factory=CallTracker)
    stage: str = ""  # name of the running stage; stamped on every recorded model call

    @classmethod
    def open(cls, settings: Settings, sweep_id: str) -> RunContext:
        return cls(settings=settings, sweep=SweepDir.open(settings.runs_dir, sweep_id), cache=DiskCache(settings.cache_path))

    def warn(self, message: str) -> None:
        """Record something a person should know about; it is written into the stage's output file."""
        self.tracker.warnings.append(message)

    def record_call(self, call: ModelCall) -> None:
        self.tracker.calls.append(call)

    def vision(self, model_stage: str) -> FallbackVision:
        """The vision model for a model stage ('detect', 'read', 'classify'), with its fallbacks."""
        from claimagent.providers.registry import build_vision  # imported here to avoid a circular import

        return build_vision(self, model_stage)

    def catalogues(self) -> list[Catalogue]:
        """The book catalogues to look titles up in, in the configured order."""
        from claimagent.providers.registry import build_catalogues  # imported here to avoid a circular import

        return build_catalogues(self)
