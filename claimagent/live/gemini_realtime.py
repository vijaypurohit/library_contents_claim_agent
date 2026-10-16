"""VoiceSession on Gemini Live: microphone audio and camera frames in, spoken audio out.

Follows the Live API pattern of the reference intake agent: realtime audio at 16 kHz, JPEG frames
as video input, input and output transcription on, the user's speech interrupting the agent, and
function calls answered with tool responses. Capture warnings are sent as text the model reacts to.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from claimagent.core.settings import Settings
from claimagent.live.session import TOOLS
from claimagent.live.voice import VoiceEvent
from claimagent.prompts import load_prompt


def live_config(settings: Settings):
    """The Live API session configuration: voice, transcription, interruption and the agent's tools."""
    from google.genai import types

    tools = [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
        for t in TOOLS])]
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=load_prompt("live_system"),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.live.voice_name))),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS),
        tools=tools,
    )


class GeminiRealtime:
    name = "gemini-live"
    speaks_warnings = True

    def __init__(self, session):
        self._session = session
        self._pending = {"user": "", "agent": ""}

    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, settings: Settings):
        """Connect to Gemini Live; the connection closes when the block ends."""
        from google import genai

        key = settings.secrets.gemini_api_key
        if not key:
            raise RuntimeError("Gemini Live needs GEMINI_API_KEY (or GOOGLE_API_KEY) in .env")
        client = genai.Client(api_key=key)
        async with client.aio.live.connect(model=settings.live.gemini_model, config=live_config(settings)) as session:
            yield cls(session)

    async def send_audio(self, pcm16: bytes) -> None:
        from google.genai import types

        await self._session.send_realtime_input(audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000"))

    async def send_frame(self, jpeg: bytes) -> None:
        from google.genai import types

        await self._session.send_realtime_input(video=types.Blob(data=jpeg, mime_type="image/jpeg"))

    async def send_text(self, text: str, from_user: bool = True, respond: bool = True) -> None:
        """A typed user turn, or a system note ("[camera] ..."); `respond=False` only adds context."""
        from google.genai import types

        await self._session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]),
                                                turn_complete=respond)

    async def send_tool_result(self, call_id: str, name: str, result: dict[str, Any]) -> None:
        from google.genai import types

        await self._session.send_tool_response(function_responses=[types.FunctionResponse(id=call_id, name=name, response=result)])

    async def events(self) -> AsyncIterator[VoiceEvent]:
        """Model output as VoiceEvents. `receive()` ends after each turn, so it is called again in a loop."""
        while True:
            async for response in self._session.receive():
                if response.tool_call and response.tool_call.function_calls:
                    for fc in response.tool_call.function_calls:
                        yield VoiceEvent("tool_call", call_id=str(fc.id or ""), name=str(fc.name or ""), args=dict(fc.args or {}))
                content = response.server_content
                if not content:
                    continue
                if content.interrupted:
                    self._pending["agent"] = ""
                    yield VoiceEvent("interrupted")
                for speaker, chunk in (("user", content.input_transcription), ("agent", content.output_transcription)):
                    if chunk and chunk.text:
                        self._pending[speaker] += chunk.text
                        yield VoiceEvent("transcript", speaker=speaker, text=self._pending[speaker])  # type: ignore[arg-type]
                if content.model_turn and not content.interrupted:
                    for part in content.model_turn.parts or []:
                        if part.inline_data and isinstance(part.inline_data.data, bytes) and \
                                (part.inline_data.mime_type or "").startswith("audio/pcm"):
                            yield VoiceEvent("audio", audio=part.inline_data.data)
                if content.turn_complete:
                    for speaker in ("user", "agent"):
                        if self._pending[speaker]:
                            yield VoiceEvent("transcript", speaker=speaker, text=self._pending[speaker], final=True)  # type: ignore[arg-type]
                            self._pending[speaker] = ""
                    yield VoiceEvent("turn_complete")

    async def close(self) -> None:
        return None  # the connection is closed by the context manager in `open`
