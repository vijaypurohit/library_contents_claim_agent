from collections.abc import Mapping

import pytest
from pydantic import BaseModel

from claimagent.core.context import RunContext
from claimagent.core.runner import MissingStageOutput, Runner
from claimagent.core.stage import Stage
from claimagent.domain.records import ModelCall


class CountOut(BaseModel):
    frames: int


class DoubleOut(BaseModel):
    doubled: int


class CountStage(Stage):
    name, order, output = "count", 1, CountOut

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> CountOut:
        ctx.record_call(ModelCall(stage=ctx.stage, provider="fake", latency_s=0.1, cost_usd=0.002))
        ctx.record_call(ModelCall(stage=ctx.stage, provider="fake", latency_s=0.0, cached=True, cost_usd=None))
        return CountOut(frames=len(ctx.sweep.frames()))


class DoubleStage(Stage):
    name, order, needs, output = "double", 2, ("count",), DoubleOut

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> DoubleOut:
        ctx.warn("example warning")
        return DoubleOut(doubled=inputs["count"].frames * 2)


class WrongTypeStage(Stage):
    name, order, output = "wrong", 3, DoubleOut

    def run(self, ctx, inputs):
        return CountOut(frames=1)


STAGES = {"count": CountStage, "double": DoubleStage}


def test_run_all_in_order_and_write_envelopes(ctx: RunContext):
    envelopes = Runner(ctx, STAGES).run()
    assert [e.stage for e in envelopes] == ["count", "double"]
    count, double = envelopes
    assert count.data == {"frames": 3}
    assert double.data == {"doubled": 6}
    assert count.cost_usd == 0.002 and count.cost_complete  # cached call is not billed
    assert double.warnings == ["example warning"]
    assert count.model_calls[0].stage == "count"
    assert ctx.sweep.stage_file(2, "double").exists()


def test_load_output_is_typed(ctx: RunContext):
    runner = Runner(ctx, STAGES)
    runner.run_stage("count")
    assert runner.load_output("count") == CountOut(frames=3)


def test_missing_input_explains_what_to_run(ctx: RunContext):
    with pytest.raises(MissingStageOutput, match="claim stage count"):
        Runner(ctx, STAGES).run_stage("double")


def test_run_from_skips_earlier_stages(ctx: RunContext):
    runner = Runner(ctx, STAGES)
    runner.run_stage("count")
    before = ctx.sweep.stage_file(1, "count").read_text()
    assert [e.stage for e in runner.run(from_stage="double")] == ["double"]
    assert ctx.sweep.stage_file(1, "count").read_text() == before


def test_tracker_is_reset_between_stages(ctx: RunContext):
    envelopes = Runner(ctx, STAGES).run()
    assert envelopes[1].model_calls == []


def test_wrong_output_type_is_rejected(ctx: RunContext):
    with pytest.raises(TypeError, match="expected DoubleOut"):
        Runner(ctx, {"wrong": WrongTypeStage}).run_stage("wrong")


def test_unknown_stage(ctx: RunContext):
    with pytest.raises(KeyError, match="unknown stage"):
        Runner(ctx, STAGES).run_stage("nope")
