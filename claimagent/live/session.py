"""One live sweep: the recorder, capture-quality warnings, the agent's tools, and the hand-off to the pipeline.

Nothing here talks to a model or a socket. The server (api/app.py) feeds frames and tool calls in
and sends whatever this returns to the browser and the voice model, so all of it is unit-testable.

- Recorder: writes every evidence frame into an ordinary sweep folder (runs/<id>/frames/) with a
  manifest, so the pipeline treats a live sweep exactly like an ingested video.
- QualityMonitor: blur, glare and pan speed per frame, against the recent frames; each kind of
  warning is repeated at most every few seconds.
- Tools the voice agent can call (TOOLS): log_observation, request_recapture, record_user_fact,
  end_sweep, sweep_status. record_user_fact writes facts.json, which the packet stage applies.
- The summary read back to the user is built in code from the claim packet: the agent never
  invents counts or totals.
- The session log (runs/<id>/live_log.jsonl): every message between the user, the agent and the
  browser except raw audio and frames, with its time in the sweep, so an adjuster can see what
  the agent said and which warnings the user was given.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
from pydantic import ValidationError

from claimagent.core.settings import LiveConfig, Settings
from claimagent.core.sessions import write_info
from claimagent.core.sweep import SweepDir, frame_name
from claimagent.domain.facts import UserFact
from claimagent.domain.packet import ClaimPacket
from claimagent.domain.records import FrameInfo, Manifest
from claimagent.report.render import money
from claimagent.vision.imaging import write_jpeg
from claimagent.vision.quality import glare, motion, sharpness

SOURCE = "live-camera"          # manifest source name of recorded frames (a video source, not photos)
MANIFEST_EVERY = 10             # frames between manifest writes, so a crash loses little

# The tools offered to the voice agent: name, description, and JSON-schema parameters.
TOOLS: list[dict[str, Any]] = [
    {"name": "log_observation",
     "description": "Note something you see that matters for the claim (a shelf finished, damage, a valuable item).",
     "parameters": {"type": "object", "properties": {
         "text": {"type": "string", "description": "What you observed, in one sentence."},
         "where": {"type": "string", "description": "Which shelf or part of the room, if known."}},
         "required": ["text"]}},
    {"name": "request_recapture",
     "description": "Ask the user to film a part again because it was blurry, dark, too fast or missed.",
     "parameters": {"type": "object", "properties": {
         "where": {"type": "string", "description": "The shelf or corner to film again."},
         "reason": {"type": "string", "description": "Why, e.g. 'spines were blurry'."}},
         "required": ["where", "reason"]}},
    {"name": "record_user_fact",
     "description": "Record a fact the user stated that changes the claim: skip a shelf, a first edition, a signed copy, "
                    "a painting that is only a print, or another note.",
     "parameters": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["skip_shelf", "first_edition", "signed", "print", "note"]},
         "target": {"type": "string", "description": "Shelf like 'A1', book like 'A1-3', item, or empty."},
         "text": {"type": "string", "description": "What the user said."}},
         "required": ["kind", "text"]}},
    {"name": "end_sweep",
     "description": "Call when the user says the sweep is finished. Starts counting, identifying and pricing in the background.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "sweep_status",
     "description": "Get the processing status, and once ready, the claim summary to read to the user word for word.",
     "parameters": {"type": "object", "properties": {}}},
]


@dataclass
class QualityWarning:
    kind: str           # "blur", "glare", "motion"
    message: str


class Recorder:
    """Saves evidence frames into a new sweep folder and keeps its manifest current."""

    def __init__(self, settings: Settings, sweep_id: str):
        self.sweep = SweepDir.create(settings.runs_dir, sweep_id)
        self.started = time.monotonic()
        self.created_at = datetime.now(UTC)
        self.frames: list[FrameInfo] = []
        self._lock = threading.Lock()

    def add(self, image: np.ndarray) -> FrameInfo:
        with self._lock:
            t = round(time.monotonic() - self.started, 2)
            index = len(self.frames)
            name = frame_name(index, t)
            write_jpeg(self.sweep.frame_path(name), image, 92)
            info = FrameInfo(name=name, index=index, t=t, source=SOURCE, width=image.shape[1], height=image.shape[0])
            self.frames.append(info)
            if len(self.frames) % MANIFEST_EVERY == 1:
                self.write_manifest()
            return info

    def write_manifest(self) -> None:
        self.sweep.write_manifest(Manifest(sweep_id=self.sweep.id, created_at=self.created_at, sources=[SOURCE],
                                           fps=0.0, frames=list(self.frames)))


class QualityMonitor:
    """Per-frame capture checks with debounced warnings."""

    def __init__(self, cfg: LiveConfig, history: int = 20):
        self.cfg = cfg
        self.recent: deque[float] = deque(maxlen=history)
        self.soft_run = 0                        # blurry frames in a row so far
        self.previous: np.ndarray | None = None
        self.last_warned: dict[str, float] = {}

    def check(self, image: np.ndarray, now: float | None = None) -> list[QualityWarning]:
        now = time.monotonic() if now is None else now
        found = []
        sharp = sharpness(image)
        soft = len(self.recent) >= 3 and sharp < self.cfg.blurry_ratio * statistics.median(self.recent)
        self.soft_run = self.soft_run + 1 if soft else 0
        if self.soft_run >= self.cfg.blur_frames:    # blur lasts; a single soft frame (a codec glitch) does not
            found.append(QualityWarning("blur", "Frames are blurry: hold the phone steady for a moment on each shelf."))
        self.recent.append(sharp)
        if glare(image) > self.cfg.glare_max:
            found.append(QualityWarning("glare", "There is glare on the spines: tilt the phone slightly or move away from the light."))
        if self.previous is not None and motion(self.previous, image) > self.cfg.motion_max:
            found.append(QualityWarning("motion", "You are moving too fast: pan more slowly so each spine stays sharp."))
        self.previous = image
        due = [w for w in found if now - self.last_warned.get(w.kind, -1e9) >= self.cfg.warn_every_s]
        for w in due:
            self.last_warned[w.kind] = now
        return due


def opening_note(settings: Settings) -> str:
    """The note that starts every session: the agent greets the user and confirms where the claim is priced."""
    return (f"[start] The session has started. The claim is priced in country {settings.locale.country} "
            f"with currency {settings.locale.currency}. Greet the user, confirm the country and currency, "
            "and explain the sweep in one or two sentences.")


def summary_for_voice(packet: ClaimPacket) -> str:
    """What the agent reads back when processing finishes. Built from the packet's computed totals only."""
    t, currency = packet.totals, packet.sweep.currency
    shelves = len({(b.unit, b.shelf) for b in packet.books})
    parts = [f"I counted {_plural(t.book_count, 'book')} on {_plural(shelves, 'shelf', 'shelves')}."]
    parts.append(f"{t.books_identified} {'has' if t.books_identified == 1 else 'have'} a title, "
                 f"{t.books_unidentified} could not be read.")
    priced = t.book_count - sum(1 for b in packet.books if b.replacement_cost.amount is None)
    if priced:
        parts.append(f"{priced} book{'s are' if priced != 1 else ' is'} priced, with a replacement cost of "
                     f"{money(t.books_replacement_cost, currency)}.")
    else:
        parts.append("No book could be priced from the shops.")
    if packet.items:
        parts.append(f"Other items: {money(t.items_replacement_cost_low, currency)} to {money(t.items_replacement_cost_high, currency)}.")
    review = len(packet.review_queue)
    parts.append(f"{_plural(review, 'point')} {'needs' if review == 1 else 'need'} a person to check; "
                 "they are listed in the report.")
    return " ".join(parts)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


@dataclass
class LiveSession:
    """State of one live sweep and the handlers for the agent's tools."""

    settings: Settings
    sweep_id: str
    origin: str = "live"                   # "live" (the web page) or "simulate" (`claim simulate`); see core/sessions.py
    voice: str = ""                        # voice mode asked for: gemini, cascaded or chat
    recorder: Recorder = field(init=False)
    quality: QualityMonitor = field(init=False)
    observations: list[dict] = field(default_factory=list)
    recaptures: list[dict] = field(default_factory=list)
    facts: list[UserFact] = field(default_factory=list)
    status: str = "recording"              # recording -> processing -> ready | failed
    summary: str | None = None
    error: str | None = None
    on_finished: Any = None                # called with the session when processing ends (set by the server)
    on_progress: Any = None                # called with a stage progress dict (core/runner.py) from the pipeline thread
    latest: np.ndarray | None = None       # most recent frame, for the provisional count
    _pipeline: threading.Thread | None = field(default=None, init=False)

    def __post_init__(self):
        self.recorder = Recorder(self.settings, self.sweep_id)
        self.quality = QualityMonitor(self.settings.live)
        write_info(self.recorder.sweep, self.origin, voice=self.voice, profile=self.settings.models.profile,
                   country=self.settings.locale.country, currency=self.settings.locale.currency,
                   calls_allowed=not self.settings.gateway.cache_only)

    def log(self, direction: str, message: dict[str, Any]) -> None:
        """Append one event to the session log. `direction`: "in" (from the browser), "out" (to it)
        or "agent-note" (a system note the voice agent was sent)."""
        entry = {"t": self._t(), "direction": direction, **message}
        with self.recorder.sweep.live_log_path.open("a") as out:
            out.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def add_frame(self, image: np.ndarray) -> list[QualityWarning]:
        """Record an evidence frame; returns the capture warnings now due."""
        if self.status != "recording":
            return []
        self.recorder.add(image)
        self.latest = image
        return self.quality.check(image)

    # ---- Tools ----

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"unknown tool {name}"}
        try:
            return handler(**args)
        except (TypeError, ValidationError) as exc:
            return {"error": f"bad arguments for {name}: {exc}"}

    def _tool_log_observation(self, text: str, where: str | None = None) -> dict:
        self.observations.append({"text": text, "where": where, "t": self._t()})
        return {"ok": True}

    def _tool_request_recapture(self, where: str, reason: str) -> dict:
        self.recaptures.append({"where": where, "reason": reason, "t": self._t()})
        return {"ok": True, "say": f"Please film {where} again: {reason}."}

    def _tool_record_user_fact(self, kind: str, text: str, target: str | None = None) -> dict:
        self.facts.append(UserFact(kind=kind, target=target or None, text=text))  # type: ignore[arg-type]
        self.recorder.sweep.facts_path.write_text("[" + ",".join(f.model_dump_json() for f in self.facts) + "]")
        if self.status in ("ready", "failed"):   # the report was already made: it needs updating to include this
            return {"ok": True, "recorded": kind, "note": "saved; the report must be updated to include it"}
        return {"ok": True, "recorded": kind}

    def _tool_end_sweep(self) -> dict:
        if self.status == "recording" and not self.recorder.frames:
            return {"error": "No camera frames were recorded, so there is nothing to process. Make sure the camera or "
                             "replay video is playing, then film the shelves and end the sweep again."}
        if self.status == "recording":
            self.start_pipeline()
        return {"status": self.status, "frames": len(self.recorder.frames),
                "say": "Thanks, that's the sweep. I'm counting, identifying and pricing now; this takes a few minutes."}

    def _tool_sweep_status(self) -> dict:
        result: dict[str, Any] = {"status": self.status}
        if self.summary:
            result["summary"] = self.summary
        if self.error:
            result["error"] = self.error
        return result

    # ---- Pipeline ----

    def start_pipeline(self, from_stage: str | None = None) -> None:
        """Run the stages in a background thread (all of them, or `from_stage` onward, to include corrections
        made after the report); `on_finished(session)` is called when it ends."""
        self.status = "processing"
        self.summary = self.error = None
        self.recorder.write_manifest()

        def work():
            from claimagent.core.context import RunContext
            from claimagent.core.runner import Runner

            try:
                ctx = RunContext.open(self.settings, self.sweep_id)
                Runner(ctx, on_progress=self.on_progress).run(from_stage)
                packet = ClaimPacket.model_validate_json(ctx.sweep.packet_path.read_text())
                self.summary = summary_for_voice(packet)
                self.status = "ready"
            except Exception as exc:  # the sweep's frames are kept; any stage can be rerun from the CLI
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "failed"
            if self.on_finished:
                self.on_finished(self)

        self._pipeline = threading.Thread(target=work, daemon=True)
        self._pipeline.start()

    def _t(self) -> float:
        return round(time.monotonic() - self.recorder.started, 2)
