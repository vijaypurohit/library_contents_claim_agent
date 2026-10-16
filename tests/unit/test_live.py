"""Live agent: recorder, quality warnings, tools, spoken summary, cascaded voice, and the WebSocket protocol."""

import asyncio
import base64
import contextlib
import json
from datetime import UTC, datetime

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from claimagent.api.app import create_app
from claimagent.core.settings import LiveConfig, Settings
from claimagent.domain.facts import load_facts
from claimagent.domain.packet import Book, BookPrice, ClaimPacket, ReviewEntry, Room, SweepInfo
from claimagent.live.cascaded import Cascaded, Reply
from claimagent.live.session import LiveSession, QualityMonitor, summary_for_voice
from claimagent.live.voice import VoiceEvent
from claimagent.vision.imaging import encode_jpeg

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def textured(seed=0, size=(480, 640)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.full((*size, 3), 90, np.uint8)
    for _ in range(300):
        x, y = int(rng.integers(0, size[1] - 20)), int(rng.integers(0, size[0] - 20))
        cv2.rectangle(img, (x, y), (x + 15, y + 15), tuple(int(c) for c in rng.integers(0, 200, 3)), -1)
    return img


# ---- Session ----

def test_recorder_writes_a_sweep_the_pipeline_can_read(settings: Settings):
    session = LiveSession(settings, "live-test")
    for i in range(3):
        session.add_frame(textured(i))
    session.recorder.write_manifest()
    manifest = session.recorder.sweep.read_manifest()
    assert [f.index for f in manifest.frames] == [0, 1, 2] and manifest.frames[0].source == "live-camera"
    assert all(session.recorder.sweep.frame_path(f.name).exists() for f in manifest.frames)


def test_quality_warnings_are_debounced():
    monitor = QualityMonitor(LiveConfig(warn_every_s=5))
    sharp = textured()
    for t in range(4):
        monitor.check(sharp, now=float(t))           # steady, sharp frames: nothing to say
    blurred = cv2.GaussianBlur(sharp, (31, 31), 0)
    assert "blur" in [w.kind for w in monitor.check(blurred, now=10.0)]
    assert "blur" not in [w.kind for w in monitor.check(blurred, now=11.0)]   # repeated within 5 s: silent
    glare = np.full_like(sharp, 255)
    assert "glare" in [w.kind for w in QualityMonitor(LiveConfig()).check(glare, now=0.0)]


def test_fast_pan_is_reported_and_a_slow_one_is_not():
    rng = np.random.default_rng(3)
    scene = cv2.resize(rng.integers(0, 255, (15, 50, 3), dtype=np.uint8), (1600, 480), interpolation=cv2.INTER_NEAREST)
    monitor = QualityMonitor(LiveConfig())
    monitor.check(scene[:, 0:640], now=0.0)
    assert "motion" not in [w.kind for w in monitor.check(scene[:, 2:642], now=1.0)]      # a couple of pixels: steady
    assert "motion" in [w.kind for w in monitor.check(scene[:, 300:940], now=2.0)]        # half a frame: too fast


def test_tools_record_facts_and_reject_bad_arguments(settings: Settings):
    session = LiveSession(settings, "live-tools")
    assert session.call_tool("record_user_fact", {"kind": "skip_shelf", "target": "A2", "text": "skip the bottom one"}) == \
        {"ok": True, "recorded": "skip_shelf"}
    assert load_facts(session.recorder.sweep.facts_path)[0].target == "A2"
    assert "error" in session.call_tool("record_user_fact", {"kind": "rare", "text": "x"})
    assert "error" in session.call_tool("no_such_tool", {})
    assert session.call_tool("request_recapture", {"where": "shelf A1", "reason": "blurry"})["say"].startswith("Please film shelf A1")


def test_summary_is_built_from_the_packet_totals():
    price = BookPrice(amount=172.0, source="s", url="https://s", retrieved_at=NOW)
    books = [Book(id="A1-1", unit="A", shelf="1", position=1, frame_ref="f.jpg", status="identified", title="T", replacement_cost=price),
             Book(id="A2-1", unit="A", shelf="2", position=1, frame_ref="f.jpg", status="unidentified")]
    packet = ClaimPacket.build(SweepInfo(id="s", captured_at=NOW, country="IN", currency="INR"), Room(), books, [],
                               [ReviewEntry(ref_id="A2-1", reason="not readable")])
    text = summary_for_voice(packet)
    assert "I counted 2 books on 2 shelves." in text and "1 has a title" in text and "₹172" in text
    assert "1 point needs a person" in text


# ---- Cascaded voice ----

class ScriptedModel:
    name = "scripted"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def generate_json(self, prompt, images, schema):
        self.prompts.append((prompt, len(images)))
        return self.replies.pop(0)


def test_cascaded_turn_with_frame_and_tool(ctx, monkeypatch):
    model = ScriptedModel([Reply(say="Noted, skipping it.", tool="record_user_fact",
                                 tool_args={"kind": "skip_shelf", "target": "A2", "text": "skip it"}),
                           Reply(say="Done.")])
    monkeypatch.setattr(ctx, "vision", lambda stage: model)
    voice = Cascaded(ctx)

    async def scenario():
        await voice.send_frame(b"\xff\xd8jpeg")
        await voice.send_text("please skip the bottom shelf")
        events = [await voice._events.get() for _ in range(4)]
        await voice.send_tool_result(events[3].call_id, "record_user_fact", {"ok": True})
        return events, await voice._events.get()

    events, after_tool = asyncio.run(scenario())
    assert [e.kind for e in events] == ["transcript", "transcript", "tool_call", "turn_complete"]
    assert events[1].text == "Noted, skipping it." and events[2].args["target"] == "A2"
    assert model.prompts[0][1] == 1 and "please skip the bottom shelf" in model.prompts[0][0]
    assert after_tool.text == "Done." and "record_user_fact returned" in model.prompts[1][0]


# ---- WebSocket protocol ----

class FakeVoice:
    """Speaks once when it hears text, calls end_sweep when told 'done'."""

    name, speaks_warnings = "fake", True

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.frames, self.texts, self.tool_results = 0, [], []

    async def send_audio(self, pcm16):
        pass

    async def send_frame(self, jpeg):
        self.frames += 1

    async def send_text(self, text, from_user=True, respond=True):
        self.texts.append(text)
        if from_user and text == "done":
            await self.queue.put(VoiceEvent("tool_call", call_id="c1", name="end_sweep"))
        elif from_user:
            await self.queue.put(VoiceEvent("audio", audio=b"\x00\x01" * 10))
            await self.queue.put(VoiceEvent("transcript", speaker="agent", text="Start at the top shelf.", final=True))

    async def send_tool_result(self, call_id, name, result):
        self.tool_results.append((name, result))

    async def events(self):
        while True:
            yield await self.queue.get()

    async def close(self):
        pass


def make_client(settings, token=None):
    voice = FakeVoice()

    @contextlib.asynccontextmanager
    async def factory(kind, settings, ctx):
        yield voice

    settings.live.provisional_every_s = 0
    app = create_app(settings, token=token, voice_factory=factory)
    return TestClient(app), voice


def frame_message(img) -> str:
    return json.dumps({"type": "frame", "data": base64.b64encode(encode_jpeg(img)).decode()})


def test_websocket_session_records_relays_and_ends(settings, monkeypatch):
    started = []
    monkeypatch.setattr(LiveSession, "start_pipeline", lambda self: (started.append(self.sweep_id), setattr(self, "status", "processing")))
    client, voice = make_client(settings)
    with client.websocket_connect("/ws/session?voice=fake") as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready" and ready["speaks_warnings"] is True
        ws.send_text(frame_message(textured(0)))
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))
        assert ws.receive_json()["type"] == "audio"
        assert ws.receive_json() == {"type": "transcript", "speaker": "agent", "text": "Start at the top shelf.", "final": True}
        ws.send_text(json.dumps({"type": "text", "text": "done"}))
        tool = ws.receive_json()
        assert tool["type"] == "tool" and tool["name"] == "end_sweep" and tool["result"]["status"] == "processing"
        assert ws.receive_json()["status"] == "processing"
    assert started and voice.frames == 1 and voice.tool_results[0][0] == "end_sweep"
    sweep = settings.runs_dir / started[0]
    assert len(list((sweep / "frames").glob("*.jpg"))) == 1 and (sweep / "manifest.json").exists()


def test_capture_warning_reaches_browser_and_agent(settings):
    client, voice = make_client(settings)
    with client.websocket_connect("/ws/session") as ws:
        ws.receive_json()
        ws.send_text(frame_message(np.full((480, 640, 3), 255, np.uint8)))   # all white: glare
        warning = ws.receive_json()
        assert warning["type"] == "warning" and warning["kind"] == "glare" and warning["speak"] is False
    assert any(t.startswith("[camera]") for t in voice.texts)


def test_access_token_is_required_when_set(settings):
    client, _ = make_client(settings, token="secret")
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config?token=secret").status_code == 200
    assert client.get("/api/config").status_code == 200       # the cookie set by the first request
    with pytest.raises(Exception):
        with TestClient(client.app).websocket_connect("/ws/session") as ws:
            ws.receive_json()


def test_config_and_settings_update(settings):
    client, _ = make_client(settings)
    assert client.get("/api/config").json()["country"] == "IN"
    assert client.put("/api/settings", json={"country": "US", "currency": "USD"}).json()["currency"] == "USD"
    assert client.put("/api/settings", json={"country": "ZZ"}).status_code == 400
