"""SweepDir: the folder that holds one sweep, and the only code that knows its layout.

    runs/<sweep_id>/
        manifest.json      what was ingested, and every frame's size, time and source
        frames/            f000123_t030.75.jpg = frame number 123, captured 30.75 s into the sweep
        stages/            one output file per stage, e.g. 3_detect.json
        debug/             images for a person to check a stage's work (overlays, contact sheets)
        evidence/          files the claim packet links to (annotated shelves, price page snapshots)
        facts.json         things the user said during the sweep ("skip the top shelf", "that one is signed")
        claim_packet.json  the final packet; report.html renders it; metrics.json has cost and latency

Other modules ask SweepDir for paths instead of building them, so the layout can change in one place.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from claimagent.domain.records import FrameRef, Manifest

FRAME_NAME = re.compile(r"^f(?P<index>\d{6})_t(?P<t>\d+\.\d{2})\.jpg$")
SWEEP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # safe as a folder name; no path tricks


def frame_name(index: int, t: float) -> str:
    """File name for a frame: its number and its time in seconds, so names sort in capture order."""
    return f"f{index:06d}_t{t:06.2f}.jpg"


def parse_frame_name(name: str) -> FrameRef:
    match = FRAME_NAME.match(name)
    if not match:
        raise ValueError(f"not a frame file name: {name!r}")
    return FrameRef(name=name, index=int(match["index"]), t=float(match["t"]))


class SweepNotFound(FileNotFoundError):
    pass


@dataclass(frozen=True)
class SweepDir:
    root: Path

    @classmethod
    def create(cls, runs_dir: Path, sweep_id: str, *, replace: bool = False) -> SweepDir:
        """Make an empty sweep folder. Refuses to overwrite an existing sweep unless `replace` is set."""
        if not SWEEP_ID.match(sweep_id):
            raise ValueError(f"invalid sweep id {sweep_id!r}: use letters, digits, '-' or '_'")
        root = runs_dir / sweep_id
        if root.exists():
            if not replace:
                raise FileExistsError(f"sweep {sweep_id!r} already exists; pass --replace to overwrite it")
            shutil.rmtree(root)
        sweep = cls(root)
        for folder in (sweep.frames_dir, sweep.stages_dir, sweep.debug_dir, sweep.evidence_dir):
            folder.mkdir(parents=True)
        return sweep

    @classmethod
    def open(cls, runs_dir: Path, sweep_id: str) -> SweepDir:
        """Open an existing sweep. A sweep exists once ingest has written its manifest."""
        root = runs_dir / sweep_id
        if not (root / "manifest.json").exists():
            raise SweepNotFound(f"no sweep {sweep_id!r} in {runs_dir}; run `claim ingest` first")
        return cls(root)

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def frames_dir(self) -> Path:
        return self.root / "frames"

    @property
    def stages_dir(self) -> Path:
        return self.root / "stages"

    @property
    def debug_dir(self) -> Path:
        return self.root / "debug"

    @property
    def evidence_dir(self) -> Path:
        return self.root / "evidence"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def facts_path(self) -> Path:
        return self.root / "facts.json"

    @property
    def packet_path(self) -> Path:
        return self.root / "claim_packet.json"

    @property
    def report_path(self) -> Path:
        return self.root / "report.html"

    @property
    def metrics_path(self) -> Path:
        return self.root / "metrics.json"

    def stage_file(self, order: int, name: str) -> Path:
        """Output file of a stage; the number prefix keeps the files in pipeline order."""
        return self.stages_dir / f"{order}_{name}.json"

    def frame_path(self, frame: FrameRef | str) -> Path:
        return self.frames_dir / (frame if isinstance(frame, str) else frame.name)

    def frames(self) -> list[FrameRef]:
        """All frames on disk, in capture order."""
        return sorted((parse_frame_name(p.name) for p in self.frames_dir.glob("f*.jpg")), key=lambda f: f.index)

    def read_manifest(self) -> Manifest:
        return Manifest.model_validate_json(self.manifest_path.read_text())

    def write_manifest(self, manifest: Manifest) -> None:
        self.manifest_path.write_text(manifest.model_dump_json(indent=2))
