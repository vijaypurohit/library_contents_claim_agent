from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from claimagent.core.cache import DiskCache
from claimagent.core.context import RunContext
from claimagent.core.settings import ModelsConfig, Secrets, Settings
from claimagent.core.sweep import SweepDir, frame_name
from claimagent.domain.records import FrameInfo, Manifest
from claimagent.vision.imaging import write_jpeg


PROVIDER_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY", "OLLAMA_API_BASE")


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    """LiteLLM loads .env into os.environ on import; keep real keys out of every test."""
    for var in PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        runs_dir=tmp_path / "runs",
        models=ModelsConfig(profile="test", profiles={"test": {"detect": ["gemini/model-a", "openai/model-b"]}}),
        secrets=Secrets(_env_file=None, GEMINI_API_KEY="g-key", OPENAI_API_KEY="o-key"),
    )


@pytest.fixture
def sweep(settings: Settings) -> SweepDir:
    """A sweep with three small synthetic frames."""
    sweep = SweepDir.create(settings.runs_dir, "test")
    frames = []
    for i in range(3):
        name = frame_name(i, i * 0.25)
        write_jpeg(sweep.frame_path(name), np.full((48, 64, 3), 40 * i, dtype=np.uint8))
        frames.append(FrameInfo(name=name, index=i, t=i * 0.25, source="synthetic", width=64, height=48))
    sweep.write_manifest(Manifest(sweep_id="test", created_at=datetime.now(UTC), sources=["synthetic"], fps=4, frames=frames))
    return sweep


@pytest.fixture
def ctx(settings: Settings, sweep: SweepDir) -> RunContext:
    return RunContext(settings=settings, sweep=sweep, cache=DiskCache(settings.cache_path))
