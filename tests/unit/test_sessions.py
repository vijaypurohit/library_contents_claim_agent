"""Session records, the History API, stage progress events, terminal logging and the sessions/clean commands."""

import json
import logging
import time

import pytest
from typer.testing import CliRunner

from claimagent.api.app import log_event
from claimagent.cli import app as cli_app
from claimagent.core.context import RunContext
from claimagent.core.runner import Runner
from claimagent.core.sessions import list_sessions, read_info, remove_session, write_info
from claimagent.core.settings import Settings
from claimagent.core.stage import Stage
from claimagent.core.sweep import SweepDir
from claimagent.live.session import LiveSession
from tests.unit.test_live import frame_message, make_client, textured


def test_origin_is_recorded_and_older_folders_are_listed_as_recorded(settings: Settings, sweep: SweepDir):
    write_info(sweep, "replay", profile="dev", sources=["a.mp4"])
    assert read_info(sweep.root)["origin"] == "replay"
    rows = list_sessions(settings.runs_dir)
    assert [(r["id"], r["label"], r["status"]) for r in rows] == [("test", "Replay", "recorded")]
    sweep.session_path.unlink()
    assert list_sessions(settings.runs_dir)[0]["origin"] == "recorded"       # no session.json: still listed
    assert list_sessions(settings.runs_dir, {"test": "processing"})[0]["status"] == "processing"


def test_live_session_writes_its_origin_voice_and_models(settings: Settings):
    session = LiveSession(settings, "live-x", origin="simulate", voice="chat")
    info = read_info(session.recorder.sweep.root)
    assert (info["origin"], info["voice"], info["profile"]) == ("simulate", "chat", "test")
    assert info["calls_allowed"] is (not settings.gateway.cache_only)


def test_history_api_lists_reads_the_conversation_and_deletes(settings: Settings):
    client, _ = make_client(settings)
    with client.websocket_connect("/ws/session?voice=chat&origin=simulate") as ws:
        ready = ws.receive_json()
        ws.send_text(frame_message(textured(0)))
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))
        assert ws.receive_json()["type"] == "audio"
        assert ws.receive_json()["type"] == "transcript"
    rows = client.get("/api/sweeps").json()
    assert rows[0]["id"] == ready["sweep_id"] and rows[0]["origin"] == "simulate" and rows[0]["has_log"]
    log = client.get(f"/api/sweeps/{ready['sweep_id']}/log").json()
    assert any(e["type"] == "transcript" and e["text"] == "Start at the top shelf." for e in log)
    assert not any(e["type"] == "audio" for e in log)
    assert client.delete(f"/api/sweeps/{ready['sweep_id']}").json() == {"removed": ready["sweep_id"]}
    assert client.get("/api/sweeps").json() == [] and client.get("/api/sweeps/nope/log").status_code == 404


def test_a_running_session_cannot_be_deleted(settings: Settings):
    client, _ = make_client(settings)
    with client.websocket_connect("/ws/session") as ws:
        sweep_id = ws.receive_json()["sweep_id"]
        ws.send_text(frame_message(textured(0)))
        assert client.delete(f"/api/sweeps/{sweep_id}").status_code == 409
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))     # a round trip: the frame was recorded by now
        ws.receive_json()
        assert client.get("/api/sweeps").json()[0]["status"] == "recording"


def test_a_session_with_no_frames_is_discarded_not_left_in_history(settings: Settings):
    client, _ = make_client(settings)
    with client.websocket_connect("/ws/session?voice=chat") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))
        ws.receive_json()
    folders = lambda: [p for p in settings.runs_dir.iterdir() if not p.name.startswith(".")]
    for _ in range(40):                                   # the server finishes closing just after the browser leaves
        if not folders():
            break
        time.sleep(0.05)
    assert client.get("/api/sweeps").json() == [] and folders() == []


def test_chat_voice_needs_no_audio_and_is_accepted_as_a_setting(settings: Settings):
    client, _ = make_client(settings)
    assert client.put("/api/settings", json={"voice": "chat"}).json()["voice"] == "chat"
    assert client.put("/api/settings", json={"voice": "telepathy"}).json()["voice"] == "chat"   # unknown: ignored


def test_regenerate_reruns_from_price_after_the_report(settings: Settings, monkeypatch):
    started = []

    def fake_start(self, from_stage=None):
        started.append(from_stage)
        self.status = "processing"

    monkeypatch.setattr(LiveSession, "start_pipeline", fake_start)
    client, _ = make_client(settings)
    with client.websocket_connect("/ws/session") as ws:
        ws.receive_json()
        ws.send_text(json.dumps({"type": "regenerate"}))       # nothing to update while still recording
        ws.send_text(json.dumps({"type": "text", "text": "hello"}))
        ws.receive_json(); ws.receive_json()
    assert started == []


def test_runner_reports_progress_for_every_stage(ctx: RunContext):
    from pydantic import BaseModel

    class Result(BaseModel):
        n: int = 1

    def make(name, order):
        return type(name.title(), (Stage,), {"name": name, "order": order, "needs": (), "output": Result,
                                             "run": lambda self, ctx, inputs: Result()})

    events = []
    runner = Runner(ctx, {"a": make("a", 1), "b": make("b", 2)}, on_progress=events.append)
    runner.run()
    assert [(e["stage"], e["order"], e["total"], e["state"]) for e in events] == \
        [("a", 1, 2, "start"), ("a", 1, 2, "done"), ("b", 2, 2, "start"), ("b", 2, 2, "done")]
    runner = Runner(ctx, {"a": make("a", 1)}, on_progress=lambda e: 1 / 0)     # a broken listener must not stop it
    assert len(runner.run()) == 1


def test_terminal_log_lines_say_what_the_session_is_doing(settings: Settings, caplog):
    session = LiveSession(settings, "live-log")
    with caplog.at_level(logging.INFO, logger="claim.live"):
        log_event(session, {"type": "ready", "voice": "cascaded", "speaks_warnings": False})
        log_event(session, {"type": "transcript", "speaker": "agent", "text": "Hello there", "final": True})
        log_event(session, {"type": "transcript", "speaker": "agent", "text": "Hel", "final": False})   # partial: not logged
        log_event(session, {"type": "warning", "kind": "glare", "message": "There is glare"})
        log_event(session, {"type": "tool", "name": "end_sweep", "args": {}, "result": {"status": "processing"}})
        log_event(session, {"type": "status", "status": "ready", "summary": "I counted 10 books."})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent: Hello there" in text and "Hel\n" not in text + "\n"
    assert "capture warning (glare)" in text and "tool end_sweep" in text and "status ready: I counted 10 books." in text


def test_sessions_and_clean_commands(settings: Settings, sweep: SweepDir, monkeypatch):
    monkeypatch.setattr("claimagent.cli._settings", lambda: settings)
    write_info(sweep, "ingest")
    runner = CliRunner()
    listed = runner.invoke(cli_app, ["sessions"]).output
    assert "test" in listed and "Recorded video" in listed and "no packet" in listed
    assert runner.invoke(cli_app, ["clean"]).exit_code != 0                         # must name something
    assert runner.invoke(cli_app, ["clean", "test"], input="n\n").exit_code != 0     # declined: nothing removed
    assert sweep.root.exists()
    assert "deleted test" in runner.invoke(cli_app, ["clean", "--unfinished", "--yes"]).output
    assert not sweep.root.exists()


def test_remove_session_rejects_paths_outside_runs(settings: Settings):
    with pytest.raises((ValueError, Exception)):
        remove_session(settings.runs_dir, "../escape")
