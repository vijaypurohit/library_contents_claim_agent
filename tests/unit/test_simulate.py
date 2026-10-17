"""Scripted live session: the reference feed's segments and faults, and a session against the real server."""

import asyncio
import contextlib
import json

import cv2
import numpy as np

from claimagent.core.settings import LiveConfig, Settings
from claimagent.live.session import LiveSession, QualityMonitor
from claimagent.live.simulate import (CameraScript, Script, Segment, UserLine, add_glare, build_feed, fit, pan_frames,
                                      run_session, start_server, write_outputs)
from claimagent.live.voice import VoiceEvent


def photo(width=1200, height=600, seed=0) -> np.ndarray:
    """A landscape picture with plenty of edges, like a shelf of spines."""
    rng = np.random.default_rng(seed)
    img = np.full((height, width, 3), 120, np.uint8)
    for x in range(0, width, 24):
        cv2.rectangle(img, (x, 50), (x + 18, height - 50), tuple(int(c) for c in rng.integers(0, 255, 3)), -1)
    return img


# ---- Feed ----

def test_fit_and_pan_give_portrait_frames_that_move_along_the_photo():
    assert fit(photo(), 90, 160).shape == (160, 90, 3)
    frames = pan_frames(photo(), 0.0, 1.0, 5, 90, 160)
    assert len(frames) == 5 and all(f.shape == (160, 90, 3) for f in frames)
    assert not np.array_equal(frames[0], frames[-1])


def test_faults_trip_the_live_quality_checks():
    """The synthetic faults are strong enough for the same checks a phone session uses."""
    sharp = fit(photo(), 360, 640)
    monitor = QualityMonitor(LiveConfig(warn_every_s=1))
    for t in range(4):
        monitor.check(sharp, now=float(t))
    assert "glare" in [w.kind for w in monitor.check(add_glare(sharp), now=10.0)]
    blurred = cv2.GaussianBlur(sharp, (0, 0), 7.0 / 2)
    assert "blur" in [w.kind for w in monitor.check(blurred, now=20.0)]


def test_build_feed_from_photo_video_hold_and_effects(tmp_path):
    still = tmp_path / "shelf.png"
    cv2.imwrite(str(still), photo())
    video = tmp_path / "pan.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 8, (180, 320))
    for i in range(16):                                   # two seconds at 8 fps
        writer.write(fit(np.roll(photo(), i * 10, axis=1), 180, 320))
    writer.release()
    cam = CameraScript(width=90, height=160, crossfade_frames=1, segments=[
        Segment(pan=str(still), seconds=1),
        Segment(hold=0.5, effect="glare", synthetic=True),
        Segment(video=str(video), speed=2, synthetic=True),
    ])
    feed = build_feed(cam, 4.0, tmp_path / "out" / "reference.mp4")
    counts = [sum(f.segment == i for f in feed) for i in range(3)]
    assert counts == [4, 2, 4]                            # 1 s pan, 0.5 s hold, 2 s of video at half the frames
    paused = build_feed(CameraScript(width=90, height=160, segments=[Segment(video=str(video), repeat=3)]), 4.0)
    assert len(paused) == 3 * 8                           # 2 s of video at 4 fps, each frame shown three times
    assert [f.synthetic for f in feed] == [False] * 4 + [True] * 6
    first = cv2.imdecode(np.frombuffer(feed[0].jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert first.shape == (160, 90, 3) and (tmp_path / "out" / "reference.mp4").stat().st_size > 0


# ---- Session ----

class ScriptedVoice:
    """Answers every user line; calls end_sweep when the user says they are done."""

    name, speaks_warnings = "scripted", False

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.frames = 0

    async def send_audio(self, pcm16):
        pass

    async def send_frame(self, jpeg):
        self.frames += 1

    async def send_text(self, text, from_user=True, respond=True):
        if from_user:
            await self.queue.put(VoiceEvent("transcript", speaker="user", text=text, final=True))
        if respond:
            await self.queue.put(VoiceEvent("transcript", speaker="agent", text=f"Noted: {text[:20]}", final=True))
        if from_user and "done" in text:
            await self.queue.put(VoiceEvent("tool_call", call_id="c1", name="end_sweep"))

    async def send_tool_result(self, call_id, name, result):
        pass

    async def events(self):
        while True:
            yield await self.queue.get()

    async def close(self):
        pass


def test_session_through_the_real_server_produces_a_transcript(settings: Settings, monkeypatch, tmp_path):
    def fake_pipeline(self):
        """Stands in for the stages: a packet-ready status straight away."""
        self.status, self.summary = "ready", "I counted 0 books."
        self.on_finished(self)

    monkeypatch.setattr(LiveSession, "start_pipeline", fake_pipeline)
    settings.live.provisional_every_s = 0
    voice = ScriptedVoice()

    @contextlib.asynccontextmanager
    async def factory(kind, settings, ctx):
        yield voice

    url, server = start_server(settings, factory)
    still = tmp_path / "shelf.png"
    cv2.imwrite(str(still), photo())
    script = Script(camera=CameraScript(width=90, height=160, segments=[Segment(pan=str(still), seconds=1)]),
                    user=[UserLine(say="Yes, India is right.", after_agent=True, start_camera=True),
                          UserLine(say="I'm done.", after_agent=True, camera_done=True, end=True)],
                    end_grace_s=5, packet_timeout_s=10)
    feed = build_feed(script.camera, 8.0)
    try:
        seen = asyncio.run(run_session(url, "cascaded", script, feed, 8.0, log=lambda line: None))
    finally:
        server.should_exit = True

    assert seen.status == "ready" and not seen.pressed_end and seen.agent_turns == 3   # greeting + one per line
    frames = list((settings.runs_dir / seen.sweep_id / "frames").glob("*.jpg"))
    assert len(frames) == len(feed) == 8 and voice.frames >= 1      # every frame recorded; ~1 per second to the model
    script_file = tmp_path / "script.yaml"
    script_file.write_text("camera: {segments: []}\nuser: []\n")
    summary = write_outputs(tmp_path / "sim", seen, script, feed, settings, script_file)
    assert summary["agent_ended_sweep"] and summary["tools"][0]["name"] == "end_sweep"
    assert summary["time_to_packet_s"] is not None and summary["summary"] == "I counted 0 books."
    transcript = (tmp_path / "sim" / "transcript.md").read_text()
    assert "| user | Yes, India is right. |" in transcript and "`end_sweep`" in transcript
    assert json.loads((tmp_path / "sim" / "events.jsonl").read_text().splitlines()[0])["type"] == "ready"
