"""VoiceSession built from parts: browser speech-to-text, any vision model, browser text-to-speech.

The browser sends what the user said as text. For each turn, the "converse" model chain is asked,
with the latest camera frame and the recent conversation, for a short reply and at most one tool
call. The reply goes back as an agent transcript, which the browser speaks. A tool result is fed
back the same way, so the model can tell the user what happened. Capture warnings are spoken by
the browser itself (speaks_warnings = False), which saves a model call per warning.

Replies are produced in background tasks, one at a time: a model turn takes seconds, and the
server's receive loop must keep recording camera frames meanwhile.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel, Field

from claimagent.core.context import RunContext
from claimagent.live.session import TOOLS
from claimagent.live.voice import VoiceEvent
from claimagent.prompts import load_prompt

HISTORY_TURNS = 12


class Reply(BaseModel):
    say: str = ""                                 # what to say now; may be empty while a tool runs
    tool: str | None = None                       # name of one tool to call, or null
    tool_args: dict[str, Any] = Field(default_factory=dict)


def turn_prompt(history: list[tuple[str, str]]) -> str:
    tools = "\n".join(f"- {t['name']}: {t['description']} Arguments: {json.dumps(t['parameters']['properties'])}" for t in TOOLS)
    lines = "\n".join(f"{who}: {text}" for who, text in history[-HISTORY_TURNS:])
    return (f"{load_prompt('live_system')}\n\nTools you can call (at most one per reply):\n{tools}\n\n"
            f"The attached image is the current camera frame, if any.\n\nConversation so far:\n{lines}\n\n"
            "Reply with what you say next (one or two short sentences) and optionally one tool call.")


class Cascaded:
    name = "cascaded"
    speaks_warnings = False

    def __init__(self, ctx: RunContext):
        self.model = ctx.vision("converse")
        self.history: list[tuple[str, str]] = []
        self.frame: bytes | None = None
        self._events: asyncio.Queue[VoiceEvent] = asyncio.Queue()
        self._lock = asyncio.Lock()   # one model turn at a time
        self._tasks: set[asyncio.Task] = set()   # replies in progress (kept so they are not garbage-collected)
        self._answered = 0            # history length the latest reply has seen

    async def send_audio(self, pcm16: bytes) -> None:
        return None  # speech is recognised in the browser

    async def send_frame(self, jpeg: bytes) -> None:
        self.frame = jpeg

    async def send_text(self, text: str, from_user: bool = True, respond: bool = True) -> None:
        """A user turn, or a system note ("[start] ...", "[count] ..."); either gets a reply when `respond`."""
        self.history.append(("user" if from_user else "system", text))
        if from_user:
            await self._events.put(VoiceEvent("transcript", speaker="user", text=text, final=True))
        if respond:
            self._reply_later()

    async def send_tool_result(self, call_id: str, name: str, result: dict[str, Any]) -> None:
        self.history.append(("tool", f"{name} returned {json.dumps(result)}"))
        self._reply_later()

    def _reply_later(self) -> None:
        """Start a reply without waiting for it; the lock in `_respond` keeps turns in order."""
        task = asyncio.create_task(self._respond(upto=len(self.history)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _respond(self, upto: int) -> None:
        """One model turn. Skipped if an earlier reply already saw everything up to `upto`
        (the user spoke twice while the model was busy: one reply answers both)."""
        async with self._lock:
            if self._answered >= upto:
                return
            self._answered = len(self.history)
            images = [self.frame] if self.frame else []
            try:
                reply = await asyncio.to_thread(self.model.generate_json, turn_prompt(self.history), images, Reply)
            except Exception as exc:  # in a background task nothing else would see it: answer and keep the session
                reply = Reply(say="Sorry, I can't reach the assistant model right now.")
                self.history.append(("system", f"model unavailable: {type(exc).__name__}: {exc}"))
            if reply.say:
                self.history.append(("agent", reply.say))
                await self._events.put(VoiceEvent("transcript", speaker="agent", text=reply.say, final=True))
            if reply.tool:
                await self._events.put(VoiceEvent("tool_call", call_id=uuid.uuid4().hex, name=reply.tool, args=reply.tool_args))
            await self._events.put(VoiceEvent("turn_complete"))

    async def events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            yield await self._events.get()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
