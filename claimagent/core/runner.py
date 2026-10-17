"""Runner: executes stages and writes their output files.

For every stage it:
  1. loads the outputs of the stages listed in `needs`
  2. runs the stage, timing it
  3. collects the model calls and warnings the stage recorded
  4. writes runs/<id>/stages/<order>_<name>.json, an "envelope" around the stage's own data

Because each stage reads only files, any stage can be rerun alone after a fix.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from claimagent.core.context import RunContext
from claimagent.core.stage import STAGES, Stage
from claimagent.domain.records import ModelCall


class StageEnvelope(BaseModel):
    """What every stage output file contains, around the stage-specific `data`."""

    stage: str
    order: int
    started_at: datetime
    latency_s: float
    model_calls: list[ModelCall]
    cost_usd: float          # sum over calls that were actually billed (cache hits are free)
    cost_complete: bool      # False when a provider did not report the price of some call
    warnings: list[str]
    data: dict[str, Any]


class MissingStageOutput(RuntimeError):
    pass


log = logging.getLogger("claim.pipeline")
ProgressCallback = Callable[[dict[str, Any]], None]


class Runner:
    def __init__(self, ctx: RunContext, stages: Mapping[str, type[Stage]] | None = None,
                 on_progress: ProgressCallback | None = None):
        """`on_progress` is called when a stage starts and when it ends, with a dict:
        {stage, order, total, state: "start" | "done", elapsed_s, model_calls}."""
        self.ctx = ctx
        self.on_progress = on_progress
        if stages is None:
            import claimagent.stages  # noqa: F401  importing the package registers the built-in stages

            stages = STAGES
        self.stages = dict(stages)

    def ordered(self) -> list[type[Stage]]:
        return sorted(self.stages.values(), key=lambda s: s.order)

    def _get(self, name: str) -> type[Stage]:
        if name not in self.stages:
            raise KeyError(f"unknown stage {name!r}; known: {[s.name for s in self.ordered()]}")
        return self.stages[name]

    def load_envelope(self, name: str) -> StageEnvelope:
        stage = self._get(name)
        path = self.ctx.sweep.stage_file(stage.order, stage.name)
        if not path.exists():
            raise MissingStageOutput(f"stage {name!r} has no output yet; run `claim stage {name} --sweep {self.ctx.sweep.id}`")
        return StageEnvelope.model_validate_json(path.read_text())

    def load_output(self, name: str) -> BaseModel:
        """A stage's saved output, parsed back into its typed model."""
        return self._get(name).output.model_validate(self.load_envelope(name).data)

    def run_stage(self, name: str) -> StageEnvelope:
        stage_cls = self._get(name)
        inputs = {need: self.load_output(need) for need in stage_cls.needs}

        self.ctx.tracker.reset()
        self.ctx.stage = stage_cls.name
        started_at = datetime.now(UTC)
        start = time.perf_counter()
        output = stage_cls().run(self.ctx, inputs)
        latency = time.perf_counter() - start

        if not isinstance(output, stage_cls.output):
            raise TypeError(f"stage {name!r} returned {type(output).__name__}, expected {stage_cls.output.__name__}")

        calls = list(self.ctx.tracker.calls)
        billed = [c for c in calls if not c.cached]
        envelope = StageEnvelope(
            stage=stage_cls.name,
            order=stage_cls.order,
            started_at=started_at,
            latency_s=round(latency, 3),
            model_calls=calls,
            cost_usd=round(sum(c.cost_usd or 0.0 for c in billed), 6),
            cost_complete=all(c.cost_usd is not None for c in billed if c.ok),
            warnings=list(self.ctx.tracker.warnings),
            data=output.model_dump(mode="json"),
        )
        self.ctx.sweep.stage_file(stage_cls.order, stage_cls.name).write_text(envelope.model_dump_json(indent=2))
        return envelope

    def run(self, from_stage: str | None = None) -> list[StageEnvelope]:
        """Run every stage in order, or only `from_stage` and the stages after it."""
        ordered = self.ordered()
        if from_stage is not None:
            start = self._get(from_stage).order
            ordered = [s for s in ordered if s.order >= start]
        done = []
        for number, stage in enumerate(ordered, start=1):
            event = {"stage": stage.name, "order": number, "total": len(ordered)}
            log.info("stage %d/%d %s started (%s)", number, len(ordered), stage.name, self.ctx.sweep.id)
            self._announce({**event, "state": "start", "elapsed_s": 0.0, "model_calls": 0})
            envelope = self.run_stage(stage.name)
            log.info("stage %d/%d %s done in %.1f s (%d model calls)", number, len(ordered), stage.name,
                     envelope.latency_s, len(envelope.model_calls))
            for warning in envelope.warnings:
                log.warning("%s: %s", stage.name, warning[:200])
            self._announce({**event, "state": "done", "elapsed_s": envelope.latency_s,
                            "model_calls": len(envelope.model_calls)})
            done.append(envelope)
        return done

    def _announce(self, event: dict[str, Any]) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(event)
            except Exception:  # a broken listener (a closed browser) must never stop the pipeline
                log.debug("progress listener failed", exc_info=True)
