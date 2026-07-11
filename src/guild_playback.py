from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Optional, Protocol

from logger import get_logger


@dataclass(frozen=True)
class TrackRequest:
    target: str
    title: Optional[str] = None
    duration: Optional[float] = None


@dataclass(frozen=True)
class Track:
    title: str
    stream_url: str
    webpage_url: Optional[str] = None
    duration: Optional[float] = None
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None


@dataclass(frozen=True)
class PlaybackOutcome:
    kind: "OutcomeKind"
    track: Optional[Track] = None
    detail: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "kind", OutcomeKind(self.kind))


class OutcomeKind(StrEnum):
    JOINED = "joined"
    STARTED = "started"
    QUEUED = "queued"
    PLAYLIST_LOADING = "playlist_loading"
    PLAYLIST_COMPLETE = "playlist_complete"
    FAILED = "failed"
    VOLUME_CHANGED = "volume_changed"
    PAUSED = "paused"
    RESUMED = "resumed"
    SKIPPED = "skipped"
    CLEARED = "cleared"
    REMOVED = "removed"


class VoiceAdapter(Protocol):
    async def ensure_connected(self, channel_id: object) -> None: ...
    async def play(
        self, track: Track, volume: float, after: Callable[[Optional[Exception]], None]
    ) -> None: ...
    def is_playing(self) -> bool: ...
    def is_paused(self) -> bool: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def stop(self) -> None: ...
    async def disconnect(self) -> None: ...
    def set_volume(self, value: float) -> None: ...


class MediaAdapter(Protocol):
    async def resolve(self, request: TrackRequest) -> Track: ...
    async def playlist_entries(self, request: TrackRequest) -> list[TrackRequest]: ...


class VolumeAdapter(Protocol):
    async def load(self, guild_id: str) -> float: ...
    async def save(self, guild_id: str, value: float) -> None: ...


class OutcomeAdapter(Protocol):
    async def publish(self, outcome: PlaybackOutcome) -> None: ...


class GuildPlayback:
    """Owns the playback lifecycle for one Discord guild."""

    def __init__(
        self,
        guild_id: str,
        voice: VoiceAdapter,
        media: MediaAdapter,
        volumes: VolumeAdapter,
        outcomes: OutcomeAdapter,
        logger=None,
    ):
        self.guild_id = guild_id
        self._voice = voice
        self._media = media
        self._volumes = volumes
        self._outcomes = outcomes
        self._logger = logger or get_logger()
        self._queue: list[Track] = []
        self._current: Optional[Track] = None
        self._volume: Optional[float] = None
        self._playback_started_at: Optional[float] = None
        self._paused_at: Optional[float] = None
        self._loader: Optional[asyncio.Task[None]] = None
        self._loading_entries: list[TrackRequest | Track] = []
        self._lock = asyncio.Lock()
        self._closed = False
        self._loop = asyncio.get_running_loop()

    async def enqueue(self, request: TrackRequest, voice_channel_id: object) -> PlaybackOutcome:
        try:
            track = await self._media.resolve(request)
        except Exception as error:
            self._logger.warning("Failed to resolve track '%s': %s", request.target, error)
            return PlaybackOutcome("failed", detail=str(error))
        return await self._enqueue_track(track, voice_channel_id)

    async def enqueue_playlist(
        self, request: TrackRequest, voice_channel_id: object
    ) -> PlaybackOutcome:
        try:
            entries = await self._media.playlist_entries(request)
        except Exception as error:
            self._logger.warning("Failed to load playlist '%s': %s", request.target, error)
            return PlaybackOutcome("failed", detail=str(error))
        if not entries:
            return PlaybackOutcome("failed", detail="Playlist has no playable tracks")

        first_track: Optional[Track] = None
        remaining = list(entries)
        while remaining and first_track is None:
            try:
                first_track = await self._media.resolve(remaining.pop(0))
            except Exception as error:
                self._logger.warning("Skipping unplayable playlist entry: %s", error)
                await self._outcomes.publish(PlaybackOutcome("failed", detail=str(error)))
                continue
        if first_track is None:
            return PlaybackOutcome("failed", detail="Playlist has no playable tracks")

        async with self._lock:
            self._ensure_open()
            await self._voice.ensure_connected(voice_channel_id)
            await self._load_volume()
            if self._current or self._voice.is_playing() or self._voice.is_paused():
                self._loading_entries.extend([first_track, *remaining])
                outcome = PlaybackOutcome("queued", first_track)
            else:
                outcome = await self._start_locked(first_track)
                self._loading_entries.extend(remaining)
            if not self._loader or self._loader.done():
                self._loader = self._loop.create_task(self._load_playlists())
        if remaining:
            await self._outcomes.publish(PlaybackOutcome("playlist_loading", detail=str(len(remaining))))
        return outcome

    async def _enqueue_track(self, track: Track, voice_channel_id: object) -> PlaybackOutcome:
        async with self._lock:
            self._ensure_open()
            await self._voice.ensure_connected(voice_channel_id)
            await self._load_volume()
            if self._current or self._voice.is_playing() or self._voice.is_paused():
                self._queue.append(track)
                return PlaybackOutcome("queued", track)
            return await self._start_locked(track)

    async def _load_playlists(self) -> None:
        while True:
            async with self._lock:
                if not self._loading_entries:
                    break
                entry = self._loading_entries.pop(0)
            try:
                track = entry if isinstance(entry, Track) else await self._media.resolve(entry)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._logger.warning("Skipping unplayable playlist entry: %s", error)
                await self._outcomes.publish(PlaybackOutcome("failed", detail=str(error)))
                continue
            async with self._lock:
                if self._closed:
                    return
                if not self._current and not self._voice.is_playing() and not self._voice.is_paused():
                    outcome = await self._start_locked(track)
                else:
                    self._queue.append(track)
                    outcome = None
            if outcome:
                await self._outcomes.publish(outcome)
        await self._outcomes.publish(PlaybackOutcome("playlist_complete"))

    @property
    def queued_tracks(self) -> tuple[Track, ...]:
        return tuple(self._queue)

    async def volume(self) -> float:
        async with self._lock:
            await self._load_volume()
            return self._volume or 1.0

    async def set_volume(self, value: float) -> PlaybackOutcome:
        if not 0 <= value <= 1.5:
            return PlaybackOutcome("failed", detail="Volume must be between 0 and 150%")
        async with self._lock:
            await self._load_volume()
            try:
                await self._volumes.save(self.guild_id, value)
            except Exception as error:
                self._logger.warning("Failed to save volume for guild %s: %s", self.guild_id, error)
                return PlaybackOutcome("failed", detail=str(error))
            self._volume = value
            self._voice.set_volume(value)
            return PlaybackOutcome("volume_changed")

    @property
    def current_track(self) -> Optional[Track]:
        return self._current

    @property
    def is_loading(self) -> bool:
        return bool(self._loading_entries or (self._loader and not self._loader.done()))

    def position(self) -> Optional[float]:
        if self._playback_started_at is None:
            return None
        return max(0.0, (self._paused_at or self._loop.time()) - self._playback_started_at)

    async def join(self, voice_channel_id: object) -> PlaybackOutcome:
        async with self._lock:
            self._ensure_open()
            await self._voice.ensure_connected(voice_channel_id)
            return PlaybackOutcome("joined")

    async def pause(self) -> PlaybackOutcome:
        async with self._lock:
            if not self._voice.is_playing():
                return PlaybackOutcome("failed", detail="No music is currently playing")
            self._voice.pause()
            self._paused_at = self._loop.time()
            return PlaybackOutcome("paused")

    async def resume(self) -> PlaybackOutcome:
        async with self._lock:
            if not self._voice.is_paused():
                return PlaybackOutcome("failed", detail="Music is not paused")
            self._voice.resume()
            if self._paused_at is not None and self._playback_started_at is not None:
                self._playback_started_at += self._loop.time() - self._paused_at
            self._paused_at = None
            return PlaybackOutcome("resumed")

    async def skip(self) -> PlaybackOutcome:
        async with self._lock:
            if not self._current:
                return PlaybackOutcome("failed", detail="Nothing is currently playing")
            self._voice.stop()
            return PlaybackOutcome("skipped", self._current)

    async def start_queued_if_idle(self) -> Optional[PlaybackOutcome]:
        """Starts a queued track after another audio feature releases the voice client."""
        async with self._lock:
            if (
                self._closed
                or self._current
                or not self._queue
                or self._voice.is_playing()
                or self._voice.is_paused()
            ):
                return None
            outcome = await self._start_locked(self._queue.pop(0))
        await self._outcomes.publish(outcome)
        return outcome

    async def remove(self, argument: str) -> PlaybackOutcome:
        async with self._lock:
            if argument.lower() == "all":
                removed = len(self._queue)
                self._queue.clear()
                self._loading_entries.clear()
                if self._loader and not self._loader.done():
                    self._loader.cancel()
                return PlaybackOutcome("cleared", detail=str(removed))
            if not self._queue:
                return PlaybackOutcome("failed", detail="The queue is empty")
            index: Optional[int] = None
            if argument.lower() == "first":
                index = 0
            elif argument.lower() == "last":
                index = len(self._queue) - 1
            else:
                try:
                    value = int(argument)
                    index = value - 1 if 1 <= value <= len(self._queue) else None
                except ValueError:
                    lowered = argument.lower()
                    index = next((i for i, track in enumerate(self._queue) if lowered in track.title.lower()), None)
            if index is None:
                return PlaybackOutcome("failed", detail="No queued track matches that selection")
            return PlaybackOutcome("removed", self._queue.pop(index))

    async def _start_locked(self, track: Track) -> PlaybackOutcome:
        await self._voice.play(track, self._volume or 1.0, self._after_track)
        self._current = track
        self._playback_started_at = self._loop.time()
        self._paused_at = None
        return PlaybackOutcome("started", track)

    def _after_track(self, error: Optional[Exception]) -> None:
        if self._closed:
            return
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(self._advance(error))
        )

    async def _advance(self, error: Optional[Exception]) -> None:
        async with self._lock:
            if self._closed:
                return
            self._current = None
            self._playback_started_at = None
            self._paused_at = None
            if error:
                self._logger.warning("Playback failed for guild %s: %s", self.guild_id, error)
                await self._outcomes.publish(PlaybackOutcome("failed", detail=str(error)))
            if not self._queue:
                return
            outcome = await self._start_locked(self._queue.pop(0))
        await self._outcomes.publish(outcome)

    async def _load_volume(self) -> None:
        if self._volume is None:
            self._volume = await self._volumes.load(self.guild_id)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Guild Playback is closed")

    async def close(self, disconnect: bool = True) -> None:
        async with self._lock:
            self._closed = True
            if self._loader and not self._loader.done():
                self._loader.cancel()
            self._loading_entries.clear()
            self._queue.clear()
            self._current = None
            self._playback_started_at = None
            self._paused_at = None
            if disconnect:
                await self._voice.disconnect()
