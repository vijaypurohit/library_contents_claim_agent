from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

from claimagent.core.settings import Settings
from claimagent.ingest import hfov_from_35mm, ingest


def make_video(path: Path, seconds: float = 2.0, fps: int = 10, size=(64, 48)) -> None:
    """Write a small synthetic video whose frames get brighter over time."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(int(seconds * fps)):
        writer.write(np.full((size[1], size[0], 3), (i * 10) % 255, dtype=np.uint8))
    writer.release()


def test_video_is_sampled_at_configured_fps(tmp_path: Path, settings: Settings):
    video = tmp_path / "clip.mp4"
    make_video(video, seconds=2.0)
    sweep = ingest([video], settings, "vid")
    manifest = sweep.read_manifest()
    assert 7 <= len(manifest.frames) <= 9  # 2 s at 4 fps
    times = [f.t for f in manifest.frames]
    assert times == sorted(times)
    assert all(sweep.frame_path(f).exists() for f in manifest.frames)


def test_photos_and_video_share_one_timeline(tmp_path: Path, settings: Settings):
    video, photo = tmp_path / "clip.mp4", tmp_path / "shelf.jpg"
    make_video(video, seconds=1.0)
    Image.new("RGB", (80, 60), "white").save(photo)
    manifest = ingest([video, photo], settings, "mixed").read_manifest()
    assert manifest.frames[-1].source == "shelf.jpg"
    assert manifest.frames[-1].t > manifest.frames[-2].t
    assert manifest.frames[-1].hfov_deg is None  # no EXIF focal length


def test_frames_are_downscaled_to_max_side(tmp_path: Path, settings: Settings):
    photo = tmp_path / "big.jpg"
    Image.new("RGB", (5000, 2500), "white").save(photo)
    settings.ingest.max_side = 1000
    frame = ingest([photo], settings, "big").read_manifest().frames[0]
    assert (frame.width, frame.height) == (1000, 500)


def test_rejects_unknown_file_types(tmp_path: Path, settings: Settings):
    doc = tmp_path / "notes.txt"
    doc.write_text("x")
    with pytest.raises(ValueError, match="unsupported"):
        ingest([doc], settings, "bad")


def test_hfov_from_focal_length():
    # 24 mm equivalent on a 3:2 landscape frame is about 74 degrees horizontally
    assert hfov_from_35mm(24, 3000, 2000) == pytest.approx(73.7, abs=0.5)
