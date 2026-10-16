"""The voice-session interface the server talks to, and the events it produces.

Two implementations:
- gemini_realtime.GeminiRealtime: Gemini Live, audio in and out, interruptions handled by the model.
- cascaded.Cascaded: the browser does speech-to-text and text-to-speech; any vision model from the
  "live" model chain answers each turn with the latest camera frame. Slower, but provider-neutral.
The server only uses the methods below, so a fake session drives the WebSocket tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

EventKind = Literal["audio", "transcript", "tool_call", "interrupted", "turn_complete"]


@dataclass
class VoiceEvent:
    kind: EventKind
    audio: bytes | None = None             # 24 kHz PCM16 for "audio"
    speaker: Literal["user", "agent"] | None = None
    text: str = ""                         # transcript text so far in this turn
    final: bool = False
    call_id: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)


class VoiceSession(Protocol):
    name: str
    speaks_warnings: bool    # True: the model is told capture warnings and says them; False: the browser speaks them

    async def send_audio(self, pcm16: bytes) -> None: ...
    async def send_frame(self, jpeg: bytes) -> None: ...
    async def send_text(self, text: str, from_user: bool = True, respond: bool = True) -> None: ...
    async def send_tool_result(self, call_id: str, name: str, result: dict[str, Any]) -> None: ...
    def events(self) -> AsyncIterator[VoiceEvent]: ...
    async def close(self) -> None: ...
