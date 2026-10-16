"""FastAPI server: the live session WebSocket, uploads, packets and reports, and the web client.

    GET  /                          the web client (web/index.html and its modules)
    GET  /api/config                what the client needs to know (voice, keys present, cache-only, country)
    PUT  /api/settings              change country/currency, voice or model profile for new sessions
    POST /api/sweeps/upload         recorded video or photos -> a sweep, processed in the background
    GET  /api/sweeps/{id}/status    recording / processing / ready / failed
    GET  /api/sweeps/{id}/packet    the claim packet
    GET  /sweeps/{id}/report.html   the report (the whole sweep folder is served, so its links work)
    WS   /ws/session?voice=...      one live sweep (protocol below)

Live protocol, all JSON:
    browser -> server  {type: "audio", data: base64 PCM16 16 kHz} | {type: "frame", data: base64 JPEG}
                       {type: "text", text} | {type: "end"}
    server -> browser  {type: "ready", sweep_id, voice} | {type: "audio", data: base64 PCM16 24 kHz}
                       {type: "transcript", speaker, text, final} | {type: "interrupted"} | {type: "turn_complete"}
                       {type: "warning", kind, message, speak} | {type: "inventory", spines, items}
                       {type: "tool", name, args, result} | {type: "status", status, summary, report}
                       {type: "error", message}

With an access token (claim serve --tunnel), every request must carry ?token=... once (it is then
kept in a cookie); without one, the server only listens on this machine.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from claimagent.core.cache import DiskCache
from claimagent.core.context import RunContext
from claimagent.core.settings import PROJECT_ROOT, Settings
from claimagent.core.sweep import SweepDir, SweepNotFound
from claimagent.live.session import LiveSession
from claimagent.live.voice import VoiceSession
from claimagent.vision.imaging import encode_jpeg, resize_max

WEB_DIR = PROJECT_ROOT / "web"
TOKEN_COOKIE = "claim_token"
MAX_MESSAGE = 2_000_000          # bytes of one WebSocket message (a 1280 px JPEG is well under this)

VoiceFactory = Callable[[str, Settings, RunContext], contextlib.AbstractAsyncContextManager[VoiceSession]]


@contextlib.asynccontextmanager
async def default_voice(kind: str, settings: Settings, ctx: RunContext):
    """Open the requested voice session: Gemini Live, or the cascaded fallback."""
    if kind == "cascaded":
        from claimagent.live.cascaded import Cascaded

        yield Cascaded(ctx)
        return
    from claimagent.live.gemini_realtime import GeminiRealtime

    async with GeminiRealtime.open(settings) as session:
        yield session


def create_app(settings: Settings, token: str | None = None, voice_factory: VoiceFactory = default_voice) -> FastAPI:
    app = FastAPI(title="Library contents claim agent")
    jobs: dict[str, dict[str, Any]] = {}       # sweep id -> status of background processing
    lock = threading.Lock()

    # ---- Access control ----

    def authorised(params, cookies) -> bool:
        return token is None or secrets.compare_digest(params.get("token") or cookies.get(TOKEN_COOKIE) or "", token)

    @app.middleware("http")
    async def check_token(request: Request, call_next):
        if not authorised(request.query_params, request.cookies):
            return JSONResponse({"detail": "open the link with its access token"}, status_code=401)
        response = await call_next(request)
        if token and request.query_params.get("token") == token:
            # Through the tunnel the page is https (cloudflared sets x-forwarded-proto); locally it may be http.
            https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="strict", secure=https)
        return response

    # ---- Client and settings ----

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/config")
    def config():
        return {"voice": settings.live.voice, "profile": settings.models.profile,
                "has_gemini_key": bool(settings.secrets.gemini_api_key), "cache_only": settings.gateway.cache_only,
                "country": settings.locale.country, "currency": settings.locale.currency,
                "frame_fps": settings.live.frame_fps, "model_fps": settings.live.model_fps,
                "profiles": sorted(settings.models.profiles)}

    @app.put("/api/settings")
    async def update_settings(request: Request):
        body = await request.json()
        if "country" in body:
            settings.locale.country = str(body["country"]).upper()
            if not settings.locale.shops_file.exists():
                raise HTTPException(400, f"no shop list for country {settings.locale.country}")
        if "currency" in body:
            settings.locale.currency = str(body["currency"]).upper()
        if body.get("voice") in ("gemini", "cascaded"):
            settings.live.voice = body["voice"]
        if body.get("profile") in settings.models.profiles:
            settings.models.profile = body["profile"]
        return config()

    # ---- Sweeps ----

    def run_pipeline(sweep_id: str) -> None:
        from claimagent.core.runner import Runner

        try:
            Runner(RunContext.open(settings, sweep_id)).run()
            jobs[sweep_id] = {"status": "ready"}
        except Exception as exc:
            jobs[sweep_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    @app.post("/api/sweeps/upload")
    async def upload(files: list[UploadFile] = File(...)):
        from claimagent.ingest import ingest

        sweep_id = "upload-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = settings.runs_dir / ".uploads" / sweep_id
        folder.mkdir(parents=True)
        paths = []
        for f in files:
            path = folder / Path(f.filename or "upload").name   # never a path from the client
            with path.open("wb") as out:
                shutil.copyfileobj(f.file, out)
            paths.append(path)
        try:
            await asyncio.to_thread(ingest, paths, settings, sweep_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with lock:
            jobs[sweep_id] = {"status": "processing"}
        threading.Thread(target=run_pipeline, args=(sweep_id,), daemon=True).start()
        return {"sweep_id": sweep_id, "status": "processing"}

    def open_sweep(sweep_id: str) -> SweepDir:
        try:
            return SweepDir.open(settings.runs_dir, sweep_id)
        except (SweepNotFound, ValueError) as exc:
            raise HTTPException(404, "no such sweep") from exc

    @app.get("/api/sweeps/{sweep_id}/status")
    def status(sweep_id: str):
        sweep = open_sweep(sweep_id)
        job = jobs.get(sweep_id) or {"status": "ready" if sweep.packet_path.exists() else "recorded"}
        return {**job, "report": f"/sweeps/{sweep_id}/report.html" if sweep.report_path.exists() else None}

    @app.get("/api/sweeps/{sweep_id}/packet")
    def packet(sweep_id: str):
        sweep = open_sweep(sweep_id)
        if not sweep.packet_path.exists():
            raise HTTPException(404, "no packet yet")
        return JSONResponse(json.loads(sweep.packet_path.read_text()))

    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/sweeps", StaticFiles(directory=settings.runs_dir), name="sweeps")
    if WEB_DIR.exists():
        app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

    # ---- Live session ----

    @app.websocket("/ws/session")
    async def live(ws: WebSocket):
        if not authorised(ws.query_params, ws.cookies):
            await ws.close(code=1008)
            return
        await ws.accept()
        voice_kind = ws.query_params.get("voice") or settings.live.voice
        session = LiveSession(settings, "live-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        ctx = RunContext(settings=settings, sweep=session.recorder.sweep, cache=DiskCache(settings.cache_path))
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()

        async def send(payload: dict) -> None:
            async with send_lock:
                with contextlib.suppress(Exception):
                    await ws.send_json(payload)

        try:
            async with voice_factory(voice_kind, settings, ctx) as voice:
                await send({"type": "ready", "sweep_id": session.sweep_id, "voice": voice.name,
                            "speaks_warnings": voice.speaks_warnings})

                def finished(s: LiveSession) -> None:
                    """Called from the pipeline thread: tell the browser and the agent."""
                    report = f"/sweeps/{s.sweep_id}/report.html" if s.status == "ready" else None
                    asyncio.run_coroutine_threadsafe(send({"type": "status", "status": s.status, "summary": s.summary,
                                                           "error": s.error, "report": report}), loop)
                    if voice.speaks_warnings:
                        note = "[status] Processing finished. Call sweep_status and read the summary to the user."
                        asyncio.run_coroutine_threadsafe(voice.send_text(note, from_user=False), loop)

                session.on_finished = finished
                await asyncio.wait([asyncio.create_task(c) for c in (
                    _client_to_server(ws, session, voice, send, settings),
                    _voice_to_client(session, voice, send),
                    _provisional(ctx, session, voice, send, settings),
                )], timeout=settings.live.max_minutes * 60, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            await send({"type": "error", "message": f"live session ended: {type(exc).__name__}: {exc}"})
        finally:
            session.recorder.write_manifest()   # the recorded frames stay usable from the CLI
            if session.status == "recording" and session.recorder.frames:
                jobs[session.sweep_id] = {"status": "recorded"}
            with contextlib.suppress(Exception):
                await ws.close()

    app.state.jobs = jobs
    return app


async def _client_to_server(ws: WebSocket, session: LiveSession, voice: VoiceSession, send, settings: Settings) -> None:
    """Browser messages: audio to the model, frames to the recorder (and some to the model), text, end."""
    last_model_frame = 0.0
    while True:
        raw = await ws.receive_text()
        if len(raw) > MAX_MESSAGE:
            await send({"type": "error", "message": "message too large"})
            continue
        message = json.loads(raw)
        kind = message.get("type")
        if kind == "audio":
            await voice.send_audio(base64.b64decode(message["data"]))
        elif kind == "frame":
            image = cv2.imdecode(np.frombuffer(base64.b64decode(message["data"]), np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                continue
            for warning in await asyncio.to_thread(session.add_frame, image):
                await send({"type": "warning", "kind": warning.kind, "message": warning.message,
                            "speak": not voice.speaks_warnings})
                if voice.speaks_warnings:
                    await voice.send_text(f"[camera] {warning.message}", from_user=False)
            now = time.monotonic()
            if now - last_model_frame >= 1 / settings.live.model_fps:
                last_model_frame = now
                await voice.send_frame(encode_jpeg(resize_max(image, settings.live.model_frame_side), 80))
        elif kind == "text" and str(message.get("text", "")).strip():
            await voice.send_text(str(message["text"])[:2000])
        elif kind == "end":
            result = session.call_tool("end_sweep", {})
            await send({"type": "status", "status": result["status"], "summary": None, "report": None})
            if voice.speaks_warnings:
                await voice.send_text("[status] The user pressed End. Tell them processing has started.", from_user=False)


async def _voice_to_client(session: LiveSession, voice: VoiceSession, send) -> None:
    """Model output to the browser; tool calls are run against the session and answered."""
    async for event in voice.events():
        if event.kind == "audio" and event.audio:
            await send({"type": "audio", "data": base64.b64encode(event.audio).decode()})
        elif event.kind == "transcript":
            await send({"type": "transcript", "speaker": event.speaker, "text": event.text, "final": event.final})
        elif event.kind in ("interrupted", "turn_complete"):
            await send({"type": event.kind})
        elif event.kind == "tool_call":
            result = session.call_tool(event.name, event.args)
            await send({"type": "tool", "name": event.name, "args": event.args, "result": result})
            if event.name == "end_sweep":
                await send({"type": "status", "status": session.status, "summary": None, "report": None})
            await voice.send_tool_result(event.call_id, event.name, result)


async def _provisional(ctx: RunContext, session: LiveSession, voice: VoiceSession, send, settings: Settings) -> None:
    """Every few seconds, a rough spine count of the latest frame (never written into the claim)."""
    from claimagent.live.provisional import count_frame

    interval = settings.live.provisional_every_s
    if interval <= 0:
        await asyncio.Event().wait()   # off: wait forever so this task never ends the session
    while True:
        await asyncio.sleep(interval)
        image = session.latest
        if image is None or session.status != "recording":
            continue
        count = await asyncio.to_thread(count_frame, ctx, image)
        if count is None:
            continue
        await send({"type": "inventory", "spines": count.spines, "items": count.items})
        await voice.send_text(f"[count] About {count.spines} book spines are in view right now (provisional).",
                              from_user=False, respond=False)


def new_token() -> str:
    return secrets.token_urlsafe(16)
