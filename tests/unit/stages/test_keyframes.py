from datetime import UTC, datetime

import cv2
import numpy as np
import pytest

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.sweep import frame_name
from claimagent.domain.records import BBox, FrameInfo, Manifest
from claimagent.stages.keyframes import KeyframesStage
from claimagent.vision.geometry import frames_overlap, iou
from claimagent.vision.imaging import write_jpeg
from claimagent.vision.quality import sharpness


def scene(width=2400, height=600, seed=0) -> np.ndarray:
    """A wide textured 'shelf' image with plenty of features to match."""
    rng = np.random.default_rng(seed)
    img = np.zeros((height, width, 3), np.uint8)
    for _ in range(400):
        x, y = rng.integers(0, width - 40), rng.integers(0, height - 40)
        cv2.rectangle(img, (int(x), int(y)), (int(x + rng.integers(10, 40)), int(y + rng.integers(10, 40))),
                      tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    return img


def write_sweep(ctx: RunContext, frames: list[tuple[np.ndarray, str]]) -> None:
    """Replace the fixture sweep's frames with (image, source) pairs."""
    for path in ctx.sweep.frames_dir.glob("*.jpg"):
        path.unlink()
    infos = []
    for i, (img, source) in enumerate(frames):
        name = frame_name(i, i * 0.25)
        write_jpeg(ctx.sweep.frame_path(name), img, 95)
        infos.append(FrameInfo(name=name, index=i, t=i * 0.25, source=source, width=img.shape[1], height=img.shape[0]))
    ctx.sweep.write_manifest(Manifest(sweep_id="test", created_at=datetime.now(UTC), sources=["x"], fps=4, frames=infos))


def test_sharp_frame_outranks_its_blurred_copy():
    img = scene()
    assert sharpness(img) > 3 * sharpness(cv2.GaussianBlur(img, (21, 21), 0))


def test_overlap_of_shifted_crops():
    img = scene()
    a, b, far = img[:, 0:800], img[:, 200:1000], img[:, 1600:2400]
    assert frames_overlap(a, b) == pytest.approx(0.75, abs=0.1)
    assert frames_overlap(a, far) == 0.0


def test_iou():
    a = BBox(x0=0, y0=0, x1=10, y1=10)
    assert iou(a, a) == 1.0
    assert iou(a, BBox(x0=5, y0=0, x1=15, y1=10)) == pytest.approx(1 / 3)
    assert iou(a, BBox(x0=20, y0=20, x1=30, y1=30)) == 0.0


def test_pan_is_split_into_views_and_sharpest_frame_kept(ctx: RunContext):
    img = scene()
    blur = lambda x: cv2.GaussianBlur(x, (15, 15), 0)
    # Panning right: frames 0-2 show the left part (frame 1 sharp, others blurred), frames 3-4 the right part.
    frames = [blur(img[:, 0:800]), img[:, 100:900], blur(img[:, 200:1000]),
              img[:, 1500:2300], blur(img[:, 1600:2400])]
    write_sweep(ctx, [(f, "clip.mp4") for f in frames])
    out = Runner(ctx, {"keyframes": KeyframesStage}).run_stage("keyframes")
    kept = [k["frame"] for k in out.data["keyframes"]]
    assert kept == [frame_name(1, 0.25), frame_name(3, 0.75)]
    assert [f["view"] for f in out.data["frames"]] == [0, 0, 0, 1, 1]
    assert (ctx.sweep.debug_dir / "keyframes.jpg").exists()


def test_photos_are_always_kept(ctx: RunContext):
    img = scene()
    write_sweep(ctx, [(img[:, 0:800], "a.heic"), (img[:, 50:850], "b.heic")])  # nearly identical photos
    out = Runner(ctx, {"keyframes": KeyframesStage}).run_stage("keyframes")
    assert [k["reason"] for k in out.data["keyframes"]] == ["photo", "photo"]


def test_view_with_only_blurry_frames_is_warned(ctx: RunContext):
    img = scene()
    blurred = cv2.GaussianBlur(img[:, 1600:2400], (31, 31), 0)
    write_sweep(ctx, [(img[:, 0:800], "clip.mp4"), (img[:, 50:850], "clip.mp4"), (blurred, "clip.mp4")])
    out = Runner(ctx, {"keyframes": KeyframesStage}).run_stage("keyframes")
    assert any("blurry and match no view" in w for w in out.warnings)
    assert len(out.data["keyframes"]) == 1          # the blurred frame is not sent to detection as a view of its own


def test_blurred_rushed_pan_does_not_split_into_many_views(ctx: RunContext):
    img = scene()
    blur = lambda x: cv2.GaussianBlur(x, (31, 31), 0)
    sharp = [img[:, 0:800], img[:, 40:840], img[:, 80:880]]
    rushed = [blur(img[:, x:x + 800]) for x in (500, 900, 1300)]      # each blurred frame shows somewhere new
    write_sweep(ctx, [(f, "clip.mp4") for f in sharp + rushed + [img[:, 1600:2400]]])
    out = Runner(ctx, {"keyframes": KeyframesStage}).run_stage("keyframes")
    assert [f["view"] for f in out.data["frames"]] == [0, 0, 0, 0, 0, 0, 1]
    assert sum("match no view" in w for w in out.warnings) == 1     # one warning for the whole blurred stretch
