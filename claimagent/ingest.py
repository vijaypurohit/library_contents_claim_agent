"""Ingest: turn recorded videos and photos into a sweep.

A sweep is a folder of numbered frames plus a manifest describing them. Every later stage reads
those frames, so a recorded video and a live session look identical to the pipeline.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from claimagent.core.sessions import write_info
from claimagent.core.settings import Settings
from claimagent.core.sweep import SweepDir, frame_name
from claimagent.domain.records import FrameInfo, Manifest
from claimagent.vision.imaging import read_image, resize_max, write_jpeg

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

FULL_FRAME_DIAGONAL_MM = 43.27  # diagonal of a 36 x 24 mm sensor, the reference for "35 mm equivalent"
EXIF_IFD = 0x8769               # EXIF sub-directory that holds camera settings
FOCAL_35MM = 0xA405             # EXIF tag: FocalLengthIn35mmFilm


def hfov_from_35mm(focal_35mm: float, width: int, height: int) -> float:
    """Horizontal field of view (degrees) from a 35 mm-equivalent focal length.

    The equivalent focal length is defined on the sensor diagonal, so we take the share of the
    diagonal that lies along the image width, then apply the pinhole-camera formula.
    """
    sensor_width_mm = FULL_FRAME_DIAGONAL_MM * width / math.hypot(width, height)
    return math.degrees(2 * math.atan(sensor_width_mm / (2 * focal_35mm)))


def _photo_hfov(path: Path, width: int, height: int) -> float | None:
    """Field of view from the photo's EXIF data, or None when the camera did not record it."""
    with Image.open(path) as pil:
        focal = pil.getexif().get_ifd(EXIF_IFD).get(FOCAL_35MM)
    return hfov_from_35mm(float(focal), width, height) if focal else None


def _video_frames(path: Path, fps: float) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (seconds, BGR frame) pairs, keeping roughly `fps` frames per second.

    OpenCV applies the video's rotation metadata itself, so portrait phone videos come out upright.
    """
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"cannot open video: {path}")
    try:
        next_t = 0.0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            t = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if t + 1e-6 >= next_t:  # skip frames until the next sampling point
                yield t, frame
                next_t += 1.0 / fps
    finally:
        capture.release()


def ingest(sources: Sequence[Path], settings: Settings, sweep_id: str | None = None, *, replace: bool = False,
           origin: str = "ingest") -> SweepDir:
    """Create a sweep from `sources` (videos and/or photos, in capture order) and return its folder.
    `origin` says why it was made (see core/sessions.py): ingest, replay or upload."""
    for src in sources:
        if not src.exists():
            raise FileNotFoundError(src)
        if src.suffix.lower() not in VIDEO_SUFFIXES | PHOTO_SUFFIXES:
            raise ValueError(f"unsupported file type: {src}")

    cfg = settings.ingest
    sweep_id = sweep_id or datetime.now(UTC).strftime("sweep-%Y%m%d-%H%M%S")
    sweep = SweepDir.create(settings.runs_dir, sweep_id, replace=replace)
    frames: list[FrameInfo] = []
    offset = 0.0  # one timeline across all sources: each source starts where the previous one ended

    def save(image: np.ndarray, t: float, src: Path, hfov: float | None) -> None:
        image = resize_max(image, cfg.max_side)
        name = frame_name(len(frames), t)
        write_jpeg(sweep.frame_path(name), image, cfg.jpeg_quality)
        h, w = image.shape[:2]
        frames.append(FrameInfo(name=name, index=len(frames), t=round(t, 2), source=src.name,
                                width=w, height=h, hfov_deg=hfov))

    for src in sources:
        if src.suffix.lower() in VIDEO_SUFFIXES:
            last_t = 0.0
            for t, image in _video_frames(src, cfg.fps):
                save(image, offset + t, src, None)  # video files carry no focal length
                last_t = t
            offset += last_t + 1.0 / cfg.fps
        else:
            image = read_image(src)
            h, w = image.shape[:2]
            save(image, offset, src, _photo_hfov(src, w, h))
            offset += 1.0  # photos are placed one second apart on the timeline

    if not frames:
        raise ValueError("no frames could be read from the sources")
    sweep.write_manifest(Manifest(
        sweep_id=sweep.id,
        created_at=datetime.now(UTC),
        sources=[str(s) for s in sources],
        fps=cfg.fps,
        frames=frames,
    ))
    write_info(sweep, origin, profile=settings.models.profile, country=settings.locale.country,
               sources=[s.name for s in sources])
    return sweep
