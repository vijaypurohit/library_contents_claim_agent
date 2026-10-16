"""Every stage on the frames of the recorded `slow` video, replaying cached model answers and shop pages.

Needs the `slow` sweep ingested earlier (runs/slow/) and the answer cache from earlier runs
(runs/.cache.sqlite); it is skipped without them. No model is called: cache-only mode stays on.
The frames are copied from runs/slow rather than decoded again, because cached answers are keyed by
the exact image bytes and a different video decoder gives slightly different pixels.
"""

import shutil

import pytest

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.settings import Settings
from claimagent.domain.packet import ClaimPacket
from claimagent.pricing import fx

SWEEP = "it-slow"


@pytest.fixture
def real_settings(monkeypatch):
    settings = Settings.load()
    if not (settings.runs_dir / "slow" / "manifest.json").exists() or not settings.cache_path.exists():
        pytest.skip("needs runs/slow/ and runs/.cache.sqlite from earlier runs")
    settings.gateway.cache_only = True
    # The day's currency rate is fetched once a day; a fixed rate keeps the test offline.
    monkeypatch.setattr("claimagent.stages.price.rate", lambda ctx, s, t: fx.Rate(95.0, "fixed", "test"))
    yield settings
    shutil.rmtree(settings.runs_dir / SWEEP, ignore_errors=True)


def test_slow_video_gives_a_valid_packet_with_ten_books(real_settings):
    source, target = real_settings.runs_dir / "slow", real_settings.runs_dir / SWEEP
    shutil.rmtree(target, ignore_errors=True)
    for folder in ("stages", "debug", "evidence"):
        (target / folder).mkdir(parents=True)
    shutil.copytree(source / "frames", target / "frames")
    shutil.copy2(source / "manifest.json", target / "manifest.json")
    ctx = RunContext.open(real_settings, SWEEP)
    Runner(ctx).run()
    packet = ClaimPacket.model_validate_json(ctx.sweep.packet_path.read_text())
    out = Runner(ctx).load_output("packet")
    assert packet.totals.book_count == 10
    assert out.valid, out.errors
    assert {b.title for b in packet.books if b.title} >= {"Word Power Made Easy"}
    assert all(b.replacement_cost.url and b.replacement_cost.snapshot for b in packet.books if b.replacement_cost.amount)
