"""The `claim` command line. Run `uv run claim --help` for the full list.

Typical use:
    uv run claim --allow-calls replay shelves.mp4           # a recording -> every stage -> runs/<name>/report.html
    uv run claim --allow-calls serve                        # the live agent in the browser
    uv run claim sessions                                   # what is in runs/, and where each came from
    uv run claim run --sweep slow --from price              # rerun price and everything after it (cached, free)

Two options you will see before the command name:
    --allow-calls   let the command call the AI models. Off by default, so nothing is billed by accident.
    --profile NAME  which models answer (gemini, openai, anthropic, groq, local, dev). Default: settings.yaml.
And one after it:
    --id NAME       a name for the run; its results go to runs/NAME/. Optional: a time-stamped name is made if omitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.settings import Settings

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Library contents claim agent.")
_state: dict = {"config": None, "allow_calls": False, "profile": None, "log_level": "info"}  # set by the global options before any command runs

ConfigOpt = Annotated[Optional[Path], typer.Option("--config", help="Settings file (default: config/settings.yaml).")]
SweepOpt = Annotated[str, typer.Option("--sweep", help="Sweep id.")]


AllowCallsOpt = Annotated[bool, typer.Option("--allow-calls", help="Call model providers even if cache-only mode is on.")]


ProfileOpt = Annotated[Optional[str], typer.Option("--profile", help="Model profile for this run, e.g. dev (default: settings).")]
LogLevelOpt = Annotated[str, typer.Option("--log-level", help="debug, info (default), warning or error: how much to print while running.")]


@app.callback()
def main(config: ConfigOpt = None, allow_calls: AllowCallsOpt = False, profile: ProfileOpt = None,
         log_level: LogLevelOpt = "info") -> None:
    from claimagent.core.log import LEVELS, setup

    if log_level.lower() not in LEVELS:
        raise typer.BadParameter(f"log level must be one of {', '.join(LEVELS)}")
    setup(log_level)           # stage progress and live-session events are printed as they happen
    _state["config"] = config
    _state["allow_calls"] = allow_calls
    _state["profile"] = profile


def _settings() -> Settings:
    settings = Settings.load(_state["config"])
    if _state["allow_calls"]:
        settings.gateway.cache_only = False
    if _state["profile"]:
        settings.models.profile = _state["profile"]
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
def reprice(
    sweep: SweepOpt,
    country: Annotated[str, typer.Option("--country", help="ISO country code with a shop list in config/locales/.")],
) -> None:
    """Price an existing inventory for another country, without re-capturing.

    Reruns price, room and packet with that country's shops and currency, and keeps a copy of the
    result as claim_packet_<country>.json and report_<country>.html next to the main ones."""
    import shutil

    from claimagent.pricing.shop import LocaleShops

    settings = _settings()
    settings.locale.country = country.upper()
    if not settings.locale.shops_file.exists():
        raise typer.BadParameter(f"no shop list for {settings.locale.country} in config/locales/")
    settings.locale.currency = LocaleShops.load(settings).currency
    ctx = RunContext.open(settings, sweep)
    _report(Runner(ctx).run(from_stage="price"))
    for name, copy in ((ctx.sweep.packet_path, f"claim_packet_{settings.locale.country}.json"),
                       (ctx.sweep.report_path, f"report_{settings.locale.country}.html")):
        shutil.copy2(name, ctx.sweep.root / copy)
    typer.echo(f"priced in {settings.locale.country} ({settings.locale.currency}): {ctx.sweep.root / f'claim_packet_{settings.locale.country}.json'}")


@app.command("eval")
def evaluate(
    sweeps: Annotated[list[str], typer.Option("--sweep", help="Sweep id; repeat for several.")],
    truth: Annotated[Path, typer.Option("--truth", help="Ground truth file.")] = Path("ground_truth/almirah/truth.yaml"),
) -> None:
    """Compare sweeps' claim packets with ground truth; writes eval/results_<sweep>.md."""
    from claimagent.core.settings import PROJECT_ROOT
    from claimagent.core.sweep import SweepDir
    from claimagent.domain.packet import ClaimPacket
    from claimagent.eval.compare import Truth, evaluate as run_eval, git_commit, load_targets, to_markdown

    settings = _settings()
    truth_data, targets, commit = Truth.load(truth), load_targets(), git_commit()
    out_dir = PROJECT_ROOT / "eval"
    out_dir.mkdir(exist_ok=True)
    for sweep_id in sweeps:
        sweep = SweepDir.open(settings.runs_dir, sweep_id)
        if not sweep.packet_path.exists():
            raise typer.BadParameter(f"sweep {sweep_id!r} has no claim packet yet; run `claim run --sweep {sweep_id}`")
        result = run_eval(ClaimPacket.model_validate_json(sweep.packet_path.read_text()), truth_data, targets, commit)
        path = out_dir / f"results_{sweep_id}.md"
        path.write_text(to_markdown(result))
        typer.echo(f"{sweep_id}: {path}")
        for m in result.measures:
            typer.echo(f"    {'ok ' if m.met else '-- ' if m.met is None else 'MISS'} {m.name}: {m.result}")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
    tunnel: Annotated[bool, typer.Option("--tunnel", help="Also open a public HTTPS link (cloudflared) protected by an access token.")] = False,
) -> None:
    """Run the live agent and web client. Use `claim --allow-calls serve` for real sessions."""
    import re
    import subprocess
    import threading

    import uvicorn

    from claimagent.api.app import create_app, new_token

    settings = _settings()
    token = new_token() if tunnel else None
    if settings.gateway.cache_only:
        typer.echo("note: cache-only mode is on, so new sweeps can't call models; start with `claim --allow-calls serve`")
    typer.echo(f"open: http://{host}:{port}/" + (f"?token={token}" if token else "") + "   (Ctrl+C to stop; what happens is printed below)")
    if tunnel:
        # cloudflared prints the public https://<name>.trycloudflare.com address on stderr.
        process = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
                                   stderr=subprocess.PIPE, text=True)

        def announce():
            for line in process.stderr:  # type: ignore[union-attr]
                match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                if match:
                    typer.echo(f"phone: {match.group(0)}/?token={token}")
                    return

        threading.Thread(target=announce, daemon=True).start()
    try:
        uvicorn.run(create_app(settings, token), host=host, port=port, log_level="warning")
    finally:
        if tunnel:
            process.terminate()


@app.command()
def replay(
    sources: Annotated[list[Path], typer.Argument(help="Recorded video and/or photos.")],
    sweep_id: Annotated[Optional[str], typer.Option("--id", help="A name for this run (default: replay-<date>-<time>). "
                                                    "Results go to runs/<name>/, and a copy to replays/<name>/<time>/.")] = None,
) -> None:
    """Ingest a recording (video or photos), run every stage, and keep a copy of all outputs for comparing runs.
    Re-using a name replaces that run."""
    import shutil
    from datetime import datetime

    from claimagent.core.settings import PROJECT_ROOT
    from claimagent.ingest import ingest as run_ingest

    settings = _settings()
    sweep_id = sweep_id or datetime.now().strftime("replay-%Y%m%d-%H%M%S")
    sweep = run_ingest(sources, settings, sweep_id, replace=True, origin="replay")
    _report(Runner(RunContext.open(settings, sweep.id)).run())
    out = PROJECT_ROOT / "replays" / sweep.id / datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copytree(sweep.stages_dir, out / "stages")
    for name in ("claim_packet.json", "report.html", "metrics.json"):
        if (sweep.root / name).exists():
            shutil.copy2(sweep.root / name, out / name)
    typer.echo(f"report: {sweep.report_path}")
    typer.echo(f"replay outputs: {out}  (diff two runs with `diff -r`)")


@app.command()
def sessions() -> None:
    """List everything in runs/: what each folder is, where it came from, and what it produced."""
    from datetime import datetime

    from claimagent.core.sessions import list_sessions

    rows = list_sessions(_settings().runs_dir)
    if not rows:
        typer.echo("no sessions yet")
    for r in rows:
        books = f"{r['books']} books" if r["books"] is not None else "no packet"
        started = datetime.fromisoformat(r["created_at"]).astimezone().strftime("%Y-%m-%d %H:%M")   # local time
        typer.echo(f"{r['id']:<28} {r['label']:<18} {started}  {r['status']:<9} {books}")
    typer.echo("remove with: claim clean <id> ...   (or: claim clean --unfinished)")


@app.command()
def clean(
    ids: Annotated[Optional[list[str]], typer.Argument(help="Session ids to delete (see `claim sessions`).")] = None,
    unfinished: Annotated[bool, typer.Option("--unfinished", help="Delete every session that has no claim packet.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete sessions from runs/ (frames, stage outputs, packet, report). Asks first."""
    from claimagent.core.sessions import list_sessions, remove_session

    settings = _settings()
    chosen = list(ids or [])
    if unfinished:
        chosen += [r["id"] for r in list_sessions(settings.runs_dir) if r["status"] != "ready" and r["id"] not in chosen]
    if not chosen:
        raise typer.BadParameter("name the sessions to delete, or use --unfinished (see `claim sessions`)")
    typer.echo("will delete: " + ", ".join(chosen))
    if not yes and not typer.confirm("Delete these sessions for good?"):
        raise typer.Abort()
    for sweep_id in chosen:
        remove_session(settings.runs_dir, sweep_id)
        typer.echo(f"deleted {sweep_id}")


@app.command()
def export(
    sweep: SweepOpt,
    out: Annotated[Optional[Path], typer.Option("--out", help="Folder (default: release/<sweep>).")] = None,
) -> None:
    """Copy a sweep's packet, report, metrics and every frame and page snapshot they reference."""
    from claimagent.core.settings import PROJECT_ROOT
    from claimagent.core.sweep import SweepDir
    from claimagent.report.export import export_sweep

    settings = _settings()
    target = out or PROJECT_ROOT / "release" / sweep
    copied, missing = export_sweep(SweepDir.open(settings.runs_dir, sweep), target)
    typer.echo(f"exported {copied} files to {target}")
    for ref in missing:
        typer.echo(f"    missing: {ref}")


@app.command()
def simulate(
    script: Annotated[Path, typer.Option("--script", help="Camera feed and user lines.")] = Path("config/live_script.yaml"),
    voice: Annotated[str, typer.Option("--voice", help="cascaded (the profile's converse models) or gemini (Gemini Live).")] = "cascaded",
    url: Annotated[Optional[str], typer.Option("--url", help="ws:// address of a running server (default: start one).")] = None,
) -> None:
    """Run a scripted live session through the real server: reference camera feed, scripted user, real models.

    Needs model calls, e.g. `claim --allow-calls --profile dev simulate`. Writes the sweep as usual,
    plus runs/<sweep>/simulation/ (reference.mp4, events.jsonl, transcript.md, simulation.json)."""
    from claimagent.live.simulate import simulate as run_simulation

    settings = _settings()
    if settings.gateway.cache_only:
        raise typer.BadParameter("a live session needs fresh model answers: run `claim --allow-calls simulate`")
    if voice not in ("cascaded", "gemini"):
        raise typer.BadParameter("voice must be cascaded or gemini")
    result = run_simulation(settings, script, voice, url, log=typer.echo)
    typer.echo(f"sweep {result['sweep_id']}: {result['status']}, packet {result['time_to_packet_s']} s after the sweep ended")
    typer.echo(f"transcript: {settings.runs_dir / result['sweep_id'] / 'simulation' / 'transcript.md'}")


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
