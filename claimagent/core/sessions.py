"""Session records: where each sweep came from, and listing, reading and removing sweeps.

Every sweep folder (runs/<id>/) can hold a small `session.json` saying how it was made:

    live      a live session in the web page (voice or chat)
    simulate  `claim simulate`: a scripted session with a reference camera feed
    upload    a video or photos uploaded in the web page
    replay    `claim replay`: a recording run through every stage
    ingest    `claim ingest`: frames only, stages run by hand
    (none)    older folders without the file are listed as "recorded"

The History tab and `claim sessions` / `claim clean` use it to show what each folder is and why it exists.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claimagent.core.sweep import SweepDir, SweepNotFound

ORIGINS = {"live": "Live session", "simulate": "Simulated session", "upload": "Upload", "replay": "Replay",
           "ingest": "Recorded video", "recorded": "Recorded"}
LOG_KINDS = ("transcript", "warning", "tool", "status", "inventory", "progress", "error")


def write_info(sweep: SweepDir, origin: str, **extra: Any) -> None:
    """Record how this sweep was made (a later call for the same sweep adds to the earlier record)."""
    info = read_info(sweep.root)
    info.update({"origin": origin, "created_at": info.get("created_at") or datetime.now(UTC).isoformat(), **extra})
    sweep.session_path.write_text(json.dumps(info, indent=2))


def read_info(root: Path) -> dict[str, Any]:
    path = root / "session.json"
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        return {}


def summarise(sweep: SweepDir, status: str | None = None) -> dict[str, Any]:
    """One row for the history list: what the folder is, when it was made, and what came out of it."""
    info = read_info(sweep.root)
    origin = info.get("origin") or "recorded"
    totals: dict[str, Any] = {}
    if sweep.packet_path.exists():
        try:
            totals = json.loads(sweep.packet_path.read_text()).get("totals", {})
        except json.JSONDecodeError:
            pass
    created = info.get("created_at") or datetime.fromtimestamp(sweep.root.stat().st_mtime, UTC).isoformat()
    return {"id": sweep.id, "origin": origin, "label": ORIGINS.get(origin, origin), "created_at": created,
            "voice": info.get("voice"), "profile": info.get("profile"), "country": info.get("country"),
            "status": status or ("ready" if sweep.packet_path.exists() else "recorded"),
            "books": totals.get("book_count"), "identified": totals.get("books_identified"),
            "report": sweep.report_path.exists(), "has_log": sweep.live_log_path.exists()}


def list_sessions(runs_dir: Path, statuses: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Every sweep folder, newest first. `statuses` overrides the status of sweeps being worked on now."""
    rows = []
    for path in sorted(runs_dir.iterdir()) if runs_dir.exists() else []:
        if path.name.startswith(".") or not (path / "manifest.json").exists():
            continue
        try:
            rows.append(summarise(SweepDir.open(runs_dir, path.name), (statuses or {}).get(path.name)))
        except (SweepNotFound, ValueError):
            continue
    return sorted(rows, key=lambda r: r["created_at"], reverse=True)


def read_log(sweep: SweepDir) -> list[dict[str, Any]]:
    """The conversation and events of a live session (what the browser was sent), oldest first."""
    if not sweep.live_log_path.exists():
        return []
    events = []
    for line in sweep.live_log_path.read_text().splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("direction") in ("out", "in") and event.get("type") in LOG_KINDS + ("text",):
            events.append(event)
    return events


def remove_session(runs_dir: Path, sweep_id: str) -> None:
    """Delete one sweep folder (frames, stages, packet, report). The id is validated by SweepDir."""
    sweep = SweepDir.open(runs_dir, sweep_id)
    shutil.rmtree(sweep.root)
