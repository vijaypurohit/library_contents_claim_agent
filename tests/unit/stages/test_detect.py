import pytest

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.domain.records import BBox
from claimagent.providers.base import ProviderUnavailable
from claimagent.providers.registry import FallbackVision
from claimagent.stages.detect import DetectStage, ModelAnswer, ModelObject, check_box, interpret, to_pixels
from claimagent.stages.keyframes import KeyframesStage

STAGES = {"keyframes": KeyframesStage, "detect": DetectStage}
W, H = 1000, 500


def obj(kind="spine", box=(100, 100, 400, 200), **extra) -> ModelObject:
    """box is (ymin, xmin, ymax, xmax) on the 0-1000 scale."""
    return ModelObject(kind=kind, label=kind, box_2d=list(box), **extra)


class FakeModel:
    """A VisionModel that returns a fixed answer, or raises."""

    def __init__(self, name, answer=None, error=None):
        self.name, self.answer, self.error, self.calls = name, answer, error, 0

    def generate_json(self, prompt, images, schema):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answer


def test_box_conversion_and_clipping():
    box = to_pixels([100, 200, 600, 1100], W, H)  # xmax beyond 1000 is clipped
    assert box == BBox(x0=200, y0=50, x1=1000, y1=300)


@pytest.mark.parametrize("kind,box,reason", [
    ("spine", BBox(x0=0, y0=0, x1=2, y1=100), "box too thin"),
    ("spine", BBox(x0=0, y0=0, x1=5, y1=10), "box too small"),
    ("spine", BBox(x0=0, y0=0, x1=600, y1=400), "spine box covers too much of the frame"),
    ("spine", BBox(x0=0, y0=0, x1=100, y1=110), "spine box is almost square"),
    ("item", BBox(x0=0, y0=0, x1=100, y1=110), None),        # items may be square
    ("spine", BBox(x0=0, y0=0, x1=30, y1=200), None),
])
def test_check_box(kind, box, reason):
    assert check_box(kind, box, W, H) == reason


def test_orientation_comes_from_box_shape_and_categories_are_normalised():
    answer = ModelAnswer(objects=[
        obj(box=(100, 100, 500, 140)),                     # tall: upright
        obj(box=(400, 100, 440, 600)),                     # wide: flat
        obj(kind="item", box=(0, 0, 900, 900), category="Wardrobe"),  # unknown category -> other
    ])
    objects = interpret(answer, frame_index=7, width=W, height=H)
    assert [o.orientation for o in objects] == ["upright", "flat", None]
    assert objects[2].category == "other"
    assert [o.id for o in objects] == ["7-1", "7-2", "7-3"]


def test_mostly_implausible_answer_falls_back_to_next_provider(ctx: RunContext):
    from claimagent.stages.detect import _answer_check

    bad = FakeModel("bad", ModelAnswer(objects=[obj(box=(0, 0, 1000, 1000)), obj(box=(0, 0, 900, 950))]))
    good = FakeModel("good", ModelAnswer(objects=[obj()]))
    chain = FallbackVision([bad, good], ctx)
    result = chain.generate_json("p", [], ModelAnswer, accept=_answer_check(W, H))
    assert result is good.answer
    assert any("answer rejected" in w for w in ctx.tracker.warnings)


def test_stage_writes_objects_and_overlays(ctx: RunContext, monkeypatch):
    answer = ModelAnswer(objects=[obj(), obj(kind="item", box=(0, 0, 800, 900), category="shelving"),
                                  obj(box=(0, 0, 1000, 1000))])  # last one is rejected but kept
    monkeypatch.setattr(ctx, "vision", lambda stage: FallbackVision([FakeModel("fake", answer)], ctx))
    runner = Runner(ctx, STAGES)
    runner.run_stage("keyframes")
    out = runner.run_stage("detect")
    frames = out.data["frames"]
    assert len(frames) == 3 and out.data["failed_frames"] == []
    objects = frames[0]["objects"]
    assert [o["valid"] for o in objects] == [True, True, False]
    assert objects[2]["reject_reason"] == "spine box covers too much of the frame"
    assert (ctx.sweep.debug_dir / f"det_{frames[0]['frame']}").exists()


def test_frame_is_listed_as_failed_when_no_provider_answers(ctx: RunContext, monkeypatch):
    broken = FakeModel("broken", error=ProviderUnavailable("quota used up"))
    monkeypatch.setattr(ctx, "vision", lambda stage: FallbackVision([broken], ctx))
    runner = Runner(ctx, STAGES)
    runner.run_stage("keyframes")
    out = runner.run_stage("detect")
    assert len(out.data["failed_frames"]) == 3
    assert all(f["detector"] is None for f in out.data["frames"])
    assert any("no provider could analyse" in w for w in out.warnings)
