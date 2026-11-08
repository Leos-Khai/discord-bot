from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol


DEFAULT_TTS_VOICE = "en-US-AriaNeural"
MAX_TTS_MESSAGE_LENGTH = 300
SPEAKER_NAME_WINDOW_SECONDS = 10.0


class TtsPreferenceStore(Protocol):
    async def get_tts_voice(self, guild_id: str, user_id: str) -> Optional[str]: ...

    async def set_tts_voice(self, guild_id: str, user_id: str, voice: str) -> None: ...


class TtsSpeaker(Protocol):
    async def speak(
        self, request: "TtsRequest", complete: Callable[[Optional[Exception]], None]
    ) -> None: ...


@dataclass(frozen=True)
class TtsRequest:
    user_id: str
    voice: str
    message: str


@dataclass(frozen=True)
class TtsEnqueueResult:
    accepted: bool
    queued: bool = False
    detail: Optional[str] = None


class TtsPreferences:
    """Loads the voice preference scoped to one user in one guild."""

    def __init__(self, store: TtsPreferenceStore, default_voice: str = DEFAULT_TTS_VOICE):
        self._store = store
        self._default_voice = default_voice

    async def voice_for(self, guild_id: str, user_id: str) -> str:
        return await self._store.get_tts_voice(guild_id, user_id) or self._default_voice

    async def set_voice(self, guild_id: str, user_id: str, voice: str) -> None:
        await self._store.set_tts_voice(guild_id, user_id, voice)


class SpeakerIntroductions:
    """Prefixes a message with the sender's name unless they were the last one heard recently."""

    def __init__(self, window_seconds: float = SPEAKER_NAME_WINDOW_SECONDS, clock: Callable[[], float] = time.monotonic):
        self._window_seconds = window_seconds
        self._clock = clock
        self._last: dict[str, tuple[str, float]] = {}

    def text(self, guild_id: str, user_id: str, name: str, message: str) -> str:
        now = self._clock()
        last = self._last.get(guild_id)
        self._last[guild_id] = (user_id, now)
        if last and last[0] == user_id and now - last[1] < self._window_seconds:
            return message
        return f"{name}, {message}"


class TtsQueue:
    """Speaks one guild's messages one after another, in the order they arrive."""

    def __init__(
        self,
        speaker: TtsSpeaker,
        *,
        max_length: int = MAX_TTS_MESSAGE_LENGTH,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_idle: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self._speaker = speaker
        self._max_length = max_length
        self._on_error = on_error
        self._on_idle = on_idle
        self._requests: list[TtsRequest] = []
        self._active: Optional[TtsRequest] = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()

    async def enqueue(self, user_id: str, voice: str, message: str) -> TtsEnqueueResult:
        message = message.strip()
        if not message:
            return TtsEnqueueResult(False, detail="Please provide a message to speak")
        if len(message) > self._max_length:
            return TtsEnqueueResult(False, detail=f"Messages are limited to {self._max_length} characters")

        async with self._lock:
            if self._closed:
                return TtsEnqueueResult(False, detail="TTS is unavailable in this voice channel")
            queued = bool(self._active or self._requests)
            self._requests.append(TtsRequest(user_id, voice, message))

        error = await self._start_next()
        if error:
            return TtsEnqueueResult(False, detail=str(error))
        return TtsEnqueueResult(True, queued=queued)

    async def clear(self) -> None:
        """Drops every message still waiting; the one already speaking is left to the caller."""
        async with self._lock:
            self._requests.clear()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._requests.clear()
            self._active = None

    async def _start_next(self) -> Optional[Exception]:
        async with self._lock:
            if self._closed or self._active or not self._requests:
                return None
            request = self._requests.pop(0)
            self._active = request
        try:
            await self._speaker.speak(request, self._complete)
        except Exception as error:
            await self._advance(error)
            return error
        return None

    def _complete(self, error: Optional[Exception] = None) -> None:
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(self._advance(error))
        )

    async def _advance(self, error: Optional[Exception]) -> None:
        async with self._lock:
            self._active = None
            if self._closed:
                return
            idle = not self._requests
        if error and self._on_error:
            self._on_error(error)
        await self._start_next()
        if idle and self._on_idle:
            await self._on_idle()
