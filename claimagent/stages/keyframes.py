"""Stage 1: Keyframes. Choose the few sharp frames that later stages will analyse.

A 3-minute sweep at 4 fps has about 720 frames, mostly near-duplicates. Sending all of them to
models would be slow and expensive, and blurry frames give wrong answers. This stage:
  1. scores every frame's sharpness
  2. groups consecutive video frames that show the same view (they overlap heavily); a blurry
     frame joins the current view instead of starting one, because blur defeats the overlap test
     and every blurred frame of a rushed pan would otherwise become a "view" of its own
  3. keeps the sharpest frame of each view
Photos are always kept: someone took each one on purpose.

Output: the kept keyframes, a score for every frame, and debug/keyframes.jpg to check by eye.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping

from pydantic import BaseModel

from claimagent.core.context import RunContext
from claimagent.core.stage import Stage, register
from claimagent.domain.records import FrameInfo
from claimagent.ingest import PHOTO_SUFFIXES
from claimagent.vision.geometry import frames_overlap
from claimagent.vision.imaging import contact_sheet, read_image, write_jpeg
from claimagent.vision.quality import sharpness


class FrameScore(BaseModel):
    frame: str
    t: float
    sharpness: float
    blurry: bool
    view: int        # frames with the same view number show (mostly) the same part of the scene
    kept: bool


class Keyframe(BaseModel):
    frame: str
    t: float
    sharpness: float
    view: int
    reason: str      # why it was kept: "sharpest of view" or "photo"


class KeyframesOutput(BaseModel):
    keyframes: list[Keyframe]
    frames: list[FrameScore]


@register
class KeyframesStage(Stage):
    name = "keyframes"
    order = 1
    output = KeyframesOutput

    def run(self, ctx: RunContext, inputs: Mapping[str, BaseModel]) -> KeyframesOutput:
        cfg = ctx.settings.keyframes
        manifest = ctx.sweep.read_manifest()
        images = {f.name: read_image(ctx.sweep.frame_path(f.name)) for f in manifest.frames}
        scores = {name: sharpness(img) for name, img in images.items()}

        # "Blurry" is relative to this sweep: lighting and camera change what a sharp frame scores.
        video_scores = [scores[f.name] for f in manifest.frames if not _is_photo(f.source)]
        blur_limit = cfg.blurry_ratio * statistics.median(video_scores) if video_scores else 0.0

        # Assign views. A photo is always its own view. A video frame joins the current view while it
        # still overlaps the view's first frame enough; otherwise it starts a new view. A blurry frame
        # cannot be matched reliably, so it never starts a view while one is open: it joins the
        # current one. If it does not overlap that view either, the camera moved on while blurred:
        # those frames are reported for re-capture, since nothing sharp shows what they passed.
        views: list[list[str]] = []
        lost: list[FrameInfo] = []          # blurry frames that match no view
        anchor = None
        for frame in manifest.frames:
            if _is_photo(frame.source):
                views.append([frame.name])
                anchor = None
                continue
            overlaps = anchor is not None and frames_overlap(images[frame.name], images[anchor]) >= cfg.new_view_overlap
            if anchor is not None and scores[frame.name] < blur_limit:
                views[-1].append(frame.name)
                if not overlaps:
                    lost.append(frame)
            elif overlaps:
                views[-1].append(frame.name)
            else:
                views.append([frame.name])
                anchor = frame.name

        view_of = {name: v for v, members in enumerate(views) for name in members}
        by_name = {f.name: f for f in manifest.frames}
        keyframes = []
        for v, members in enumerate(views):
            best = max(members, key=lambda n: scores[n])
            photo = _is_photo(by_name[best].source)
            keyframes.append(Keyframe(frame=best, t=by_name[best].t, sharpness=round(scores[best], 1), view=v,
                                      reason="photo" if photo else "sharpest of view"))
            if not photo and scores[best] < blur_limit:
                ctx.warn(f"view {v} (t={by_name[best].t:.2f}s) has only blurry frames; re-capture recommended")
        for first, last in _runs(lost, manifest.frames):
            ctx.warn(f"frames at t={first.t:.2f}-{last.t:.2f}s are blurry and match no view (the camera moved while "
                     "blurred); re-capture recommended")

        kept = {k.frame for k in keyframes}
        frame_scores = [
            FrameScore(frame=f.name, t=f.t, sharpness=round(scores[f.name], 1),
                       blurry=not _is_photo(f.source) and scores[f.name] < blur_limit,
                       view=view_of[f.name], kept=f.name in kept)
            for f in manifest.frames
        ]

        # Contact sheet of the kept frames, captioned with time and sharpness, for checking by eye.
        sheet = contact_sheet([images[k.frame] for k in keyframes],
                              [f"t={k.t:.2f}s  sharp={k.sharpness:.0f}" for k in keyframes])
        write_jpeg(ctx.sweep.debug_dir / "keyframes.jpg", sheet)
        return KeyframesOutput(keyframes=keyframes, frames=frame_scores)


def _runs(frames: list[FrameInfo], order: list[FrameInfo]) -> list[tuple[FrameInfo, FrameInfo]]:
    """Consecutive stretches of `frames` within the sweep's frame order, as (first, last) pairs."""
    position = {f.name: i for i, f in enumerate(order)}
    runs: list[list[FrameInfo]] = []
    for frame in frames:
        if runs and position[frame.name] == position[runs[-1][-1].name] + 1:
            runs[-1].append(frame)
        else:
            runs.append([frame])
    return [(run[0], run[-1]) for run in runs]


def _is_photo(source: str) -> bool:
    return any(source.lower().endswith(suffix) for suffix in PHOTO_SUFFIXES)
