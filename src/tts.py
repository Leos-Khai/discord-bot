from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol


DEFAULT_TTS_VOICE = "en-US-AriaNeural"
MAX_TTS_MESSAGE_LENGTH = 300
TTS_COOLDOWN_SECONDS = 5.0


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


class TtsQueue:
    """Serializes one guild's spoken messages and enforces request limits."""

    def __init__(
        self,
        speaker: TtsSpeaker,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_length: int = MAX_TTS_MESSAGE_LENGTH,
        cooldown_seconds: float = TTS_COOLDOWN_SECONDS,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_idle: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self._speaker = speaker
        self._clock = clock
        self._max_length = max_length
        self._cooldown_seconds = cooldown_seconds
        self._on_error = on_error
        self._on_idle = on_idle
        self._requests: list[TtsRequest] = []
        self._pending_users: set[str] = set()
        self._last_requested: dict[str, float] = {}
        self._active: Optional[TtsRequest] = None
        self._closed = False
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()

    async def enqueue(
        self, user_id: str, voice: str, message: str, *, bypass_cooldown: bool = False
    ) -> TtsEnqueueResult:
        message = message.strip()
        if not message:
            return TtsEnqueueResult(False, detail="Please provide a message to speak")
        if len(message) > self._max_length:
            return TtsEnqueueResult(False, detail=f"Messages are limited to {self._max_length} characters")

        now = self._clock()
        async with self._lock:
            if self._closed:
                return TtsEnqueueResult(False, detail="TTS is unavailable in this voice channel")
            if user_id in self._pending_users:
                return TtsEnqueueResult(False, detail="already have a TTS message pending")
            if not bypass_cooldown and user_id in self._last_requested:
                remaining = self._cooldown_seconds - (now - self._last_requested[user_id])
                if remaining > 0:
                    return TtsEnqueueResult(
                        False,
                        detail=f"Please wait {int(remaining + 0.999)} more second(s) before using TTS again",
                    )
            request = TtsRequest(user_id, voice, message)
            queued = bool(self._active or self._requests)
            self._requests.append(request)
            self._pending_users.add(user_id)
            self._last_requested[user_id] = now

        error = await self._start_next()
        if error:
            return TtsEnqueueResult(False, detail=str(error))
        return TtsEnqueueResult(True, queued=queued)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._requests.clear()
            self._pending_users.clear()
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
            if self._active:
                self._pending_users.discard(self._active.user_id)
                self._active = None
            if self._closed:
                return
            idle = not self._requests
        if error and self._on_error:
            self._on_error(error)
        await self._start_next()
        if idle and self._on_idle:
            await self._on_idle()
