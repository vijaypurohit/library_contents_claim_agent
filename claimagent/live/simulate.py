"""Scripted live session: a reference camera feed and a scripted user, through the real server.

`claim simulate` checks the whole live path without a person in front of the camera:

    reference feed (config/live_script.yaml)          scripted user lines
            |  JPEG frames at live.frame_fps, real time      |  typed turns, sent when their conditions hold
            v                                                v
    WebSocket /ws/session  ->  the same server, voice session, quality checks, provisional count,
                               tools and pipeline that a browser session uses
            |
            v
    runs/<sweep>/  claim_packet.json, report.html, live_log.jsonl, and simulation/ with
                   reference.mp4 (what the camera showed), events.jsonl (every server message),
                   transcript.md (the conversation, warnings and tool calls) and simulation.json

The camera feed is built from recordings of the test space. Faults the agent must catch (glare,
blur, a rushed pan) are added on top and marked synthetic in the script and the transcript.
Nothing here is mocked: the voice model, detection, spine reading and pricing all run for real, so
the run needs `--allow-calls`. With `--voice gemini` the user's lines are sent as typed turns to
Gemini Live, which exercises the Live API bridge; `--voice cascaded` uses the profile's
"converse" models (for example Claude Haiku in the dev profile).
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from pydantic import BaseModel, Field

from claimagent.core.settings import PROJECT_ROOT, Settings
from claimagent.vision.imaging import encode_jpeg, read_image

AGENT_WAIT_S = 90.0        # longest wait for the agent to answer before the next line is sent anyway
SEND_SIDE = 1920           # long side of a sent frame, as the web client sends it (live.evidence_side)
SEND_QUALITY = 85          # JPEG quality, as the web client sends it
GLARE_RADIUS = 0.5         # glare blob radius, as a share of the frame width (the inner half is blown out)
BLUR_SIGMA = 7.0           # Gaussian blur of a lost-focus segment, at 720 px width
SHAKE_PX = 3.0             # hand shake while holding still, in pixels


# ---- Script ----

class Segment(BaseModel):
    """One piece of the camera feed (see config/live_script.yaml for the meaning of each field)."""
    video: str | None = None
    speed: int = Field(1, ge=1)
    repeat: int = Field(1, ge=1)       # each frame shown this many times, with hand shake: a person pausing on each view
    skip_s: float = Field(0.0, ge=0)   # leave out the first N seconds of the recording (a camera still finding focus)
    pan: str | None = None
    hold: float | None = None
    start: float = Field(0.0, ge=0, le=1, alias="from")
    to: float = Field(1.0, ge=0, le=1)
    seconds: float = 0.0
    effect: str | None = None          # "glare" or "blur"
    synthetic: bool = False
    note: str = ""


class CameraScript(BaseModel):
    width: int = 720
    height: int = 1280
    crossfade_frames: int = 2
    segments: list[Segment]


class UserLine(BaseModel):
    say: str
    after_agent: bool = False
    camera_t: float | None = None
    camera_done: bool = False
    start_camera: bool = False
    end: bool = False


class Script(BaseModel):
    camera: CameraScript
    user: list[UserLine]
    end_grace_s: float = 60.0
    packet_timeout_s: float = 1500.0

    @classmethod
    def load(cls, path: Path) -> Script:
        return cls.model_validate(yaml.safe_load(path.read_text()))


# ---- Reference camera feed ----

@dataclass
class FeedFrame:
    jpeg: bytes          # what is sent, encoded like the web client does
    segment: int         # index into the script's segments
    synthetic: bool


def fit(image: np.ndarray, width: int, height: int) -> np.ndarray:
    """Centre-crop to the target aspect ratio, then resize: how a phone screen frames a picture."""
    h, w = image.shape[:2]
    target = width / height
    if w / h > target:                      # too wide: cut the sides
        keep = int(round(h * target))
        x = (w - keep) // 2
        image = image[:, x:x + keep]
    else:                                   # too tall: cut top and bottom
        keep = int(round(w / target))
        y = (h - keep) // 2
        image = image[y:y + keep]
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def pan_frames(photo: np.ndarray, start: float, end: float, n: int, width: int, height: int) -> list[np.ndarray]:
    """A portrait window of the photo's full height sliding from `start` to `end` across it."""
    h, w = photo.shape[:2]
    window = min(w, int(round(h * width / height)))
    frames = []
    for k in range(n):
        share = start + (end - start) * (k / max(1, n - 1))
        x = int(round(share * (w - window)))
        frames.append(cv2.resize(photo[:, x:x + window], (width, height), interpolation=cv2.INTER_AREA))
    return frames


def shaken(image: np.ndarray, k: int) -> np.ndarray:
    """The frame moved by a few pixels, as a hand holding a phone still does."""
    dx, dy = SHAKE_PX * np.sin(k * 1.3), SHAKE_PX * np.cos(k * 0.9)
    matrix = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(image, matrix, (image.shape[1], image.shape[0]), borderMode=cv2.BORDER_REPLICATE)


def add_glare(image: np.ndarray) -> np.ndarray:
    """A bright reflection: a round patch whose centre is blown out to white, fading at its edge."""
    h, w = image.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    distance = np.hypot(xx - 0.6 * w, yy - 0.35 * h) / (GLARE_RADIUS * w)
    alpha = np.clip(2.0 * (1.0 - distance), 0.0, 1.0)[..., None]    # 1 (white) inside half the radius
    return (image * (1 - alpha) + 255 * alpha).astype(np.uint8)


def apply_effect(image: np.ndarray, effect: str | None) -> np.ndarray:
    if effect == "glare":
        return add_glare(image)
    if effect == "blur":
        return cv2.GaussianBlur(image, (0, 0), BLUR_SIGMA * image.shape[1] / 720)
    if effect:
        raise ValueError(f"unknown effect {effect!r}: use glare or blur")
    return image


def segment_frames(seg: Segment, previous: np.ndarray | None, fps: float, width: int, height: int) -> list[np.ndarray]:
    """The raw frames of one segment, before effects."""
    if seg.video:
        from claimagent.ingest import _video_frames

        frames = [fit(img, width, height) for t, img in _video_frames(PROJECT_ROOT / seg.video, fps) if t >= seg.skip_s]
        frames = frames[::seg.speed]
        return [shaken(f, k) if k else f for f in frames for k in range(seg.repeat)]
    if seg.pan:
        n = max(2, int(round(seg.seconds * fps)))
        return pan_frames(read_image(PROJECT_ROOT / seg.pan), seg.start, seg.to, n, width, height)
    if seg.hold is not None:
        if previous is None:
            raise ValueError("a hold segment needs a segment before it")
        return [shaken(previous, k) for k in range(max(1, int(round(seg.hold * fps))))]
    raise ValueError("a segment needs video, pan or hold")


def build_feed(cam: CameraScript, fps: float, video_out: Path | None = None) -> list[FeedFrame]:
    """Every frame of the reference sweep, encoded for sending; optionally also written as a video."""
    writer = None
    if video_out:
        video_out.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(str(video_out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (cam.width, cam.height))
    feed: list[FeedFrame] = []
    last: np.ndarray | None = None          # last frame shown, before any effect (holds start from it)
    shown: np.ndarray | None = None         # last frame shown, as sent
    try:
        for index, seg in enumerate(cam.segments):
            raw = segment_frames(seg, last, fps, cam.width, cam.height)
            for k, frame in enumerate(raw):
                out = apply_effect(frame, seg.effect)
                if shown is not None and k < cam.crossfade_frames and seg.hold is None:
                    alpha = (k + 1) / (cam.crossfade_frames + 1)      # blend the cut over a few frames
                    out = cv2.addWeighted(out, alpha, shown, 1 - alpha, 0)
                feed.append(FeedFrame(encode_jpeg(out, SEND_QUALITY, SEND_SIDE), index, seg.synthetic))
                if writer is not None:
                    writer.write(out)
                shown = out
            last = raw[-1]
    finally:
        if writer is not None:
            writer.release()
    return feed


# ---- The session ----

@dataclass
class Observed:
    """What the simulated user has seen and heard so far."""
    started: float = field(default_factory=time.monotonic)
    events: list[dict[str, Any]] = field(default_factory=list)
    agent_turns: int = 0
    sweep_id: str = ""
    voice: str = ""
    status: str = "recording"
    ended_at: float | None = None        # seconds into the session when processing started
    finished_at: float | None = None     # seconds into the session when the packet was ready (or failed)
    camera_elapsed: float = 0.0
    camera_done: bool = False
    pressed_end: bool = False
    audio: bytearray = field(default_factory=bytearray)

    def now(self) -> float:
        return round(time.monotonic() - self.started, 2)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(settings: Settings, voice_factory=None) -> tuple[str, Any]:
    """The real FastAPI app on a free local port, in a background thread. Returns (ws url, server).
    `voice_factory` replaces the voice backends (tests only)."""
    import uvicorn

    from claimagent.api.app import create_app

    port = free_port()
    app = create_app(settings, voice_factory=voice_factory) if voice_factory else create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the server did not start")
        time.sleep(0.05)
    return f"ws://127.0.0.1:{port}", server


async def run_session(url: str, voice: str, script: Script, feed: list[FeedFrame], fps: float,
                      log=print) -> Observed:
    """Connect, stream the feed and say the lines, then wait for the claim packet."""
    from websockets.asyncio.client import connect

    seen = Observed()
    camera_go = asyncio.Event()
    finished = asyncio.Event()

    async with connect(f"{url}/ws/session?voice={voice}&origin=simulate", max_size=None) as ws:

        async def send(payload: dict) -> None:
            await ws.send(json.dumps(payload))

        async def receive() -> None:
            async for raw in ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "audio":
                    seen.audio.extend(base64.b64decode(message["data"]))
                    continue
                message["t"] = seen.now()
                seen.events.append(message)
                if kind == "ready":
                    seen.sweep_id, seen.voice = message["sweep_id"], message["voice"]
                elif kind == "transcript" and message.get("final"):
                    if message["speaker"] == "agent":
                        seen.agent_turns += 1
                    log(f"[{message['t']:6.1f}s] {message['speaker']}: {message['text']}")
                elif kind == "warning":
                    log(f"[{message['t']:6.1f}s] warning ({message['kind']}): {message['message']}")
                elif kind == "tool":
                    if message["name"] == "end_sweep" and seen.ended_at is None:
                        seen.ended_at = message["t"]           # the sweep ends when the agent ends it
                    log(f"[{message['t']:6.1f}s] tool {message['name']} {json.dumps(message['args'])}")
                elif kind == "inventory":
                    log(f"[{message['t']:6.1f}s] provisional count: {message['spines']} spines")
                elif kind == "status":
                    seen.status = message["status"]
                    if seen.status == "processing" and seen.ended_at is None:
                        seen.ended_at = message["t"]
                    if (seen.status == "ready" and message.get("summary")) or seen.status == "failed":   # the final status
                        seen.finished_at = message["t"]
                        log(f"[{message['t']:6.1f}s] status {seen.status}: {message.get('summary') or message.get('error')}")
                        finished.set()
                elif kind == "error":
                    log(f"[{message['t']:6.1f}s] error: {message['message']}")

        async def camera() -> None:
            await camera_go.wait()
            start = time.monotonic()
            for i, frame in enumerate(feed):
                if seen.status != "recording":
                    break
                await asyncio.sleep(max(0.0, start + i / fps - time.monotonic()))   # real time, like a phone
                await send({"type": "frame", "data": base64.b64encode(frame.jpeg).decode()})
                seen.camera_elapsed = (i + 1) / fps
            seen.camera_done = True

        async def wait_until(condition, limit: float) -> bool:
            deadline = time.monotonic() + limit
            while not condition():
                if time.monotonic() > deadline:
                    return False
                await asyncio.sleep(0.2)
            return True

        async def user() -> None:
            answered = 0                       # agent turns seen when the previous line was sent
            for line in script.user:
                if line.after_agent and not await wait_until(lambda: seen.agent_turns > answered, AGENT_WAIT_S):
                    log(f"[{seen.now():6.1f}s] (no answer from the agent within {AGENT_WAIT_S:g} s; going on)")
                if line.camera_t is not None:
                    await wait_until(lambda: seen.camera_elapsed >= line.camera_t, 3600)
                if line.camera_done:
                    await wait_until(lambda: seen.camera_done, 3600)
                answered = seen.agent_turns
                await send({"type": "text", "text": line.say})
                if line.start_camera:
                    camera_go.set()
                if line.end and not await wait_until(lambda: seen.status != "recording", script.end_grace_s):
                    log(f"[{seen.now():6.1f}s] (the agent did not end the sweep; pressing End)")
                    seen.pressed_end = True
                    await send({"type": "end"})

        tasks = [asyncio.create_task(c) for c in (receive(), camera(), user())]
        try:
            # The packet clock starts when the sweep ends, however long the sweep itself took.
            await wait_until(lambda: seen.ended_at is not None or tasks[0].done(), 3600)
            try:
                await asyncio.wait_for(finished.wait(), timeout=script.packet_timeout_s)
            except TimeoutError:
                seen.status = "timeout"
                log(f"[{seen.now():6.1f}s] no packet within {script.packet_timeout_s:g} s of the sweep ending")
            if voice == "gemini" and finished.is_set():   # Gemini is told processing finished and reads the summary
                turns = seen.agent_turns
                await wait_until(lambda: seen.agent_turns > turns, AGENT_WAIT_S)
        finally:
            for task in tasks:
                task.cancel()
    return seen


# ---- Outputs ----

def write_outputs(out: Path, seen: Observed, script: Script, feed: list[FeedFrame], settings: Settings,
                  script_path: Path) -> dict[str, Any]:
    """events.jsonl, transcript.md and simulation.json in `out`; returns the simulation summary."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "script.yaml").write_text(script_path.read_text())
    with (out / "events.jsonl").open("w") as f:
        for event in seen.events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    if seen.audio:
        with wave.open(str(out / "agent_audio.wav"), "wb") as w:   # Gemini's voice, its turns back to back
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(bytes(seen.audio))

    packet_path = settings.runs_dir / seen.sweep_id / "claim_packet.json"
    packet = json.loads(packet_path.read_text()) if packet_path.exists() else None
    final = [e for e in seen.events if e.get("type") == "transcript" and e.get("final")]
    summary = {
        "sweep_id": seen.sweep_id, "voice": seen.voice, "profile": settings.models.profile,
        "models": {stage: settings.models.chain(stage) for stage in ("converse", "detect", "read", "classify")},
        "frames_sent": len(feed), "synthetic_frames": sum(f.synthetic for f in feed),
        "camera_s": round(len(feed) / settings.live.frame_fps, 2),
        "agent_turns": sum(e["speaker"] == "agent" for e in final), "user_turns": sum(e["speaker"] == "user" for e in final),
        "warnings": [{"t": e["t"], "kind": e["kind"]} for e in seen.events if e.get("type") == "warning"],
        "tools": [{"t": e["t"], "name": e["name"], "args": e["args"]} for e in seen.events if e.get("type") == "tool"],
        "provisional_counts": [e["spines"] for e in seen.events if e.get("type") == "inventory"],
        "agent_ended_sweep": not seen.pressed_end,
        "status": seen.status,
        "time_to_packet_s": round(seen.finished_at - seen.ended_at, 1) if seen.finished_at and seen.ended_at else None,
        "summary": next((e.get("summary") for e in reversed(seen.events) if e.get("type") == "status" and e.get("summary")), None),
        "packet": {"valid": None, "totals": packet["totals"], "review_queue": len(packet["review_queue"])} if packet else None,
    }
    stage9 = settings.runs_dir / seen.sweep_id / "stages" / "9_packet.json"
    if packet and stage9.exists():
        summary["packet"]["valid"] = json.loads(stage9.read_text())["data"]["valid"]
    (out / "simulation.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    (out / "transcript.md").write_text(transcript_markdown(seen, script, feed, summary))
    return summary


def transcript_markdown(seen: Observed, script: Script, feed: list[FeedFrame], summary: dict[str, Any]) -> str:
    """The session as a readable timeline, with the reference feed and the results."""
    lines = [f"# Simulated live session {seen.sweep_id}", "",
             f"Voice: {summary['voice']}; model profile: {summary['profile']}; "
             f"converse models: {', '.join(summary['models']['converse'])}.",
             "The user's lines are scripted and sent as recognised speech; the camera is the reference feed below. "
             "Everything else (agent replies, warnings, tool calls, the pipeline) ran for real.", "",
             "## Reference camera feed", "", "| # | Seconds | Source | Synthetic | What it shows |", "|---|---|---|---|---|"]
    fps = len(feed) / summary["camera_s"] if summary["camera_s"] else 4.0
    for i, seg in enumerate(script.camera.segments):
        n = sum(f.segment == i for f in feed)
        source = seg.video or seg.pan or "hold"
        extra = f" ({seg.effect})" if seg.effect else (f" (x{seg.speed})" if seg.speed > 1 else "")
        lines.append(f"| {i + 1} | {n / fps:.1f} | `{source}`{extra} | {'yes' if seg.synthetic else 'no'} | {seg.note} |")
    lines += ["", "## Timeline", "", "| t (s) | Who | What |", "|---|---|---|"]
    for e in seen.events:
        kind = e.get("type")
        if kind == "transcript" and e.get("final"):
            lines.append(f"| {e['t']:.1f} | {e['speaker']} | {e['text']} |")
        elif kind == "warning":
            lines.append(f"| {e['t']:.1f} | camera check | {e['kind']}: {e['message']} |")
        elif kind == "tool":
            lines.append(f"| {e['t']:.1f} | tool | `{e['name']}` {json.dumps(e['args'], ensure_ascii=False)} → {json.dumps(e['result'], ensure_ascii=False)} |")
        elif kind == "inventory":
            lines.append(f"| {e['t']:.1f} | on screen | provisional count: {e['spines']} spines |")
        elif kind == "status":
            text = e.get("summary") or e.get("error") or ""
            lines.append(f"| {e['t']:.1f} | status | {e['status']}{': ' + text if text else ''} |")
        elif kind == "error":
            lines.append(f"| {e['t']:.1f} | error | {e['message']} |")
    if seen.pressed_end:
        lines += ["", "The agent did not call end_sweep after the user said they were done; the simulator pressed End."]
    lines += ["", "## Result", "", "```json", json.dumps({k: summary[k] for k in (
        "frames_sent", "synthetic_frames", "camera_s", "agent_turns", "agent_ended_sweep", "status",
        "time_to_packet_s", "packet")}, indent=2, ensure_ascii=False), "```", ""]
    return "\n".join(lines)


def simulate(settings: Settings, script_path: Path, voice: str = "cascaded", url: str | None = None,
             log=print) -> dict[str, Any]:
    """Run one scripted session end to end; returns the summary written to simulation.json."""
    script = Script.load(script_path)
    fps = settings.live.frame_fps
    staging = settings.runs_dir / ".simulation"
    feed = build_feed(script.camera, fps, staging / "reference.mp4")
    log(f"reference feed: {len(feed)} frames, {len(feed) / fps:.1f} s ({sum(f.synthetic for f in feed)} with synthetic faults)")
    server = None
    if url is None:
        url, server = start_server(settings)
    try:
        seen = asyncio.run(run_session(url, voice, script, feed, fps, log))
    finally:
        if server is not None:
            server.should_exit = True
    out = settings.runs_dir / seen.sweep_id / "simulation"
    out.mkdir(parents=True, exist_ok=True)
    (staging / "reference.mp4").replace(out / "reference.mp4")
    return write_outputs(out, seen, script, feed, settings, script_path)
