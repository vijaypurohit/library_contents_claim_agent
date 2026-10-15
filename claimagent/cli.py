"""The `claim` command line. Run `uv run claim --help` for the full list.

Typical use:
    uv run claim ingest tests-local-data/video_slow.mp4 --id slow   # recorded video -> sweep "slow"
    uv run claim stage detect --sweep slow                 # run one stage
    uv run claim run --sweep slow --from price             # rerun price and everything after it
    uv run claim --allow-calls stage detect --sweep slow   # allow real model calls (cache-only is the default)
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.settings import Settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Library contents claim agent.")
_state: dict = {"config": None, "allow_calls": False}  # set by the global options before any command runs

ConfigOpt = Annotated[Optional[Path], typer.Option("--config", help="Settings file (default: config/settings.yaml).")]
SweepOpt = Annotated[str, typer.Option("--sweep", help="Sweep id.")]


AllowCallsOpt = Annotated[bool, typer.Option("--allow-calls", help="Call model providers even if cache-only mode is on.")]


@app.callback()
def main(config: ConfigOpt = None, allow_calls: AllowCallsOpt = False) -> None:
    _state["config"] = config
    _state["allow_calls"] = allow_calls


def _settings() -> Settings:
    settings = Settings.load(_state["config"])
    if _state["allow_calls"]:
        settings.gateway.cache_only = False
    return settings


@app.command()
def ingest(
    sources: Annotated[list[Path], typer.Argument(help="Video and/or photo files, in capture order.")],
    sweep_id: Annotated[Optional[str], typer.Option("--id", help="Sweep id (default: timestamp).")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Overwrite an existing sweep with this id.")] = False,
) -> None:
    """Create a sweep from recorded video or photos."""
    from claimagent.ingest import ingest as run_ingest

    sweep = run_ingest(sources, _settings(), sweep_id, replace=replace)
    manifest = sweep.read_manifest()
    typer.echo(f"sweep {sweep.id}: {len(manifest.frames)} frames in {sweep.frames_dir}")


@app.command()
def stage(name: Annotated[str, typer.Argument(help="Stage name.")], sweep: SweepOpt) -> None:
    """Run one stage."""
    envelope = Runner(RunContext.open(_settings(), sweep)).run_stage(name)
    _report([envelope])


@app.command()
def run(
    sweep: SweepOpt,
    from_stage: Annotated[Optional[str], typer.Option("--from", help="Start from this stage.")] = None,
) -> None:
    """Run all stages, or every stage from one onward."""
    _report(Runner(RunContext.open(_settings(), sweep)).run(from_stage))


@app.command()
def marker(out: Annotated[Path, typer.Option("--out", help="PDF to write.")] = Path("marker.pdf")) -> None:
    """Write a printable A4 scale card."""
    from claimagent.scale.marker import save_marker_pdf

    settings = _settings()
    save_marker_pdf(out, settings.marker)
    typer.echo(f"wrote {out} ({settings.marker.size_cm:g} cm marker)")


def _report(envelopes) -> None:
    """One line per stage: time, model calls and cost ("+" means some call had no published price)."""
    for env in envelopes:
        cost = f"${env.cost_usd:.4f}" + ("" if env.cost_complete else "+")
        typer.echo(f"{env.order} {env.stage:<10} {env.latency_s:7.2f}s  {len(env.model_calls):3d} calls  {cost}")
        for warning in env.warnings:
            typer.echo(f"    warning: {warning}")


if __name__ == "__main__":
    app()
