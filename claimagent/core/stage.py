"""Stage: one step of the post-sweep pipeline (keyframes, detect, price, ...).

A stage only describes its work. It declares which earlier stages it needs, what it returns,
and implements `run`. The Runner does everything else: loading inputs, timing, cost tracking
and writing the output file. That keeps every stage short and testable on its own.

Example:
    @register
    class DetectStage(Stage):
        name = "detect"
        order = 3                 # position in the pipeline and in the output file name
        needs = ("keyframes",)    # outputs this stage reads
        output = DetectOutput     # Pydantic model of what it returns

        def run(self, ctx, inputs):
            keyframes = inputs["keyframes"]
            ...
            return DetectOutput(...)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from claimagent.core.context import RunContext


class Stage(ABC):
    name: ClassVar[str]
    order: ClassVar[int]
    needs: ClassVar[tuple[str, ...]] = ()
    output: ClassVar[type[BaseModel]]

    @abstractmethod
    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> BaseModel:
        """Compute this stage's output from earlier stage outputs (`inputs`, keyed by stage name)."""


# All built-in stages, by name. Filled by the @register decorator when claimagent.stages is imported.
STAGES: dict[str, type[Stage]] = {}


def register(cls: type[Stage]) -> type[Stage]:
    if cls.name in STAGES and STAGES[cls.name] is not cls:
        raise ValueError(f"stage {cls.name!r} registered twice")
    STAGES[cls.name] = cls
    return cls
