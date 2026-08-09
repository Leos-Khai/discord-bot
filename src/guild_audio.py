from __future__ import annotations

from array import array
import os
from pathlib import Path
import tempfile
import threading
from typing import Callable, Optional, Protocol

import discord
import edge_tts

from guild_playback import Track
from tts import TtsEnqueueResult, TtsQueue, TtsRequest, TtsSpeaker


PCM_FRAME_BYTES = 960 * 2 * 2
DUCKED_MUSIC_VOLUME = 0.20
FADE_FRAMES = 50
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1",
    "options": "-vn -bufsize 512k -maxrate 128k",
}


class _TemporaryAudioSource(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, filename: str):
        self._source = source
        self._filename = filename
        self._cleaned = False

    @property
    def error(self):
        return getattr(self._source, "_current_error", None)

    def read(self):
        return self._source.read()

    def is_opus(self):
        return self._source.is_opus()

    def cleanup(self):
        if not self._cleaned:
            self._cleaned = True
            self._source.cleanup()
            Path(self._filename).unlink(missing_ok=True)


class _GuildAudioMixer(discord.AudioSource):
    """A mutable source that keeps music and speech independently alive."""

    def __init__(self):
        self._music = None
        self._music_complete = None
        self._speech = None
        self._speech_complete = None
        self._music_paused = False
        self._music_factor = 1.0
        self._retired = []
        self._lock = threading.Lock()

    def set_music(self, source, complete):
        with self._lock:
            if self._music:
                # The audio thread may be mid-read on the source being replaced, so it
                # is torn down there instead of here.
                self._retired.append(self._music)
            self._music = source
            self._music_complete = complete

    def add_speech(self, source, complete):
        with self._lock:
            if self._speech:
                raise RuntimeError("A TTS message is already playing")
            self._speech = source
            self._speech_complete = complete

    def pause_music(self):
        self._music_paused = True

    def resume_music(self):
        self._music_paused = False

    def skip_music(self):
        self._finish_music(None)

    def set_music_volume(self, value):
        with self._lock:
            if isinstance(self._music, discord.PCMVolumeTransformer):
                self._music.volume = value

    def read(self):
        self._drain_retired()
        with self._lock:
            music, speech = self._music, self._speech
        music_frame = b"" if self._music_paused or not music else music.read()
        speech_frame = b"" if not speech else speech.read()
        if music and not music_frame and not self._music_paused:
            self._finish_music(getattr(music, "_current_error", None), source=music)
            # A seek may have swapped in a replacement mid-read. Re-reading tells a
            # real ending apart from a displaced source, whose silence means nothing.
            with self._lock:
                music = self._music
        if speech and not speech_frame:
            self._finish_speech(speech.error)
            speech = None
        if not music_frame and not speech_frame:
            return b"" if not (music or speech) else b"\x00" * PCM_FRAME_BYTES
        target = DUCKED_MUSIC_VOLUME if music and speech else 1.0
        step = (1.0 - DUCKED_MUSIC_VOLUME) / FADE_FRAMES
        self._music_factor += max(-step, min(step, target - self._music_factor))
        size = max(len(music_frame), len(speech_frame), PCM_FRAME_BYTES)
        return self._mix(music_frame.ljust(size, b"\x00"), speech_frame.ljust(size, b"\x00"), self._music_factor)

    def _finish_music(self, error, source=None):
        with self._lock:
            # A source displaced by a seek can still run dry on the audio thread; it
            # must not complete the source that replaced it.
            if source is not None and source is not self._music:
                return
            music, complete = self._music, self._music_complete
            self._music = self._music_complete = None
        if music:
            music.cleanup()
        if complete:
            complete(error)

    def _finish_speech(self, error):
        with self._lock:
            speech, complete = self._speech, self._speech_complete
            self._speech = self._speech_complete = None
        if speech:
            speech.cleanup()
        if complete:
            complete(error)

    @staticmethod
    def _mix(music, speech, music_factor):
        music_samples, speech_samples = array("h"), array("h")
        music_samples.frombytes(music)
        speech_samples.frombytes(speech)
        for index, sample in enumerate(music_samples):
            music_samples[index] = max(-32768, min(32767, int(sample * music_factor) + speech_samples[index]))
        return music_samples.tobytes()

    def is_opus(self):
        return False

    def _drain_retired(self):
        with self._lock:
            retired, self._retired = self._retired, []
        for source in retired:
            source.cleanup()

    def cleanup(self):
        self._drain_retired()
        self._finish_music(RuntimeError("Guild audio stopped"))
        self._finish_speech(RuntimeError("Guild audio stopped"))


class DiscordGuildAudioOutput:
    """Discord adapter used only by GuildAudioCoordinator."""

    def __init__(self, guild: discord.Guild):
        self._guild = guild
        self._mixer: Optional[_GuildAudioMixer] = None

    async def ensure_connected(self, channel):
        client = self._guild.voice_client
        if client is None:
            await channel.connect()
        elif client.channel != channel:
            await client.move_to(channel)

    def _active_mixer(self):
        client = self._guild.voice_client
        if not client or not client.is_connected():
            raise RuntimeError("Voice client unavailable for playback")
        if self._mixer is None:
            self._mixer = _GuildAudioMixer()
        return client, self._mixer

    @staticmethod
    def _start_if_idle(client, mixer):
        if not client.is_playing():
            client.play(mixer, after=lambda error: None)

    async def start_playback(self, track, volume, after, start_at=0.0):
        options = FFMPEG_OPTIONS
        if start_at > 0:
            # -ss ahead of the input makes ffmpeg seek the stream rather than decode
            # and discard everything up to the target.
            options = {
                **FFMPEG_OPTIONS,
                "before_options": f"-ss {start_at:.3f} {FFMPEG_OPTIONS['before_options']}",
            }
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track.stream_url, **options), volume=volume
        )
        client, mixer = self._active_mixer()
        mixer.set_music(source, after)
        self._start_if_idle(client, mixer)

    async def start_speech(self, request, complete):
        descriptor, filename = tempfile.mkstemp(prefix="discord-tts-", suffix=".mp3")
        os.close(descriptor)
        try:
            await edge_tts.Communicate(request.message, request.voice).save(filename)
        except Exception:
            Path(filename).unlink(missing_ok=True)
            raise
        source = _TemporaryAudioSource(discord.FFmpegPCMAudio(filename), filename)
        try:
            client, mixer = self._active_mixer()
            mixer.add_speech(source, complete)
            self._start_if_idle(client, mixer)
        except Exception:
            source.cleanup()
            raise

    def pause_playback(self):
        if self._mixer:
            self._mixer.pause_music()

    def resume_playback(self):
        if self._mixer:
            self._mixer.resume_music()

    def skip_playback(self):
        if self._mixer:
            self._mixer.skip_music()

    def set_volume(self, value):
        if self._mixer:
            self._mixer.set_music_volume(value)

    async def stop_all(self):
        client = self._guild.voice_client
        if client:
            client.stop()
        self._mixer = None

    async def disconnect(self):
        client = self._guild.voice_client
        if client and client.is_connected():
            await client.disconnect()


class GuildAudioRegistry:
    """Keeps one Guild Audio Coordination module for each Discord guild."""

    def __init__(self, logger=None):
        self._logger = logger
        self._coordinators: dict[str, GuildAudioCoordinator] = {}

    def for_guild(self, guild):
        guild_id = str(guild.id)
        coordinator = self._coordinators.get(guild_id)
        if coordinator is None:
            coordinator = GuildAudioCoordinator(
                DiscordGuildAudioOutput(guild),
                on_tts_error=(lambda error: self._logger.warning("TTS playback failed: %s", error)) if self._logger else None,
            )
            self._coordinators[guild_id] = coordinator
        return coordinator

    async def discard(self, guild_id: str):
        coordinator = self._coordinators.pop(guild_id, None)
        if coordinator:
            await coordinator.close()

    async def close_all(self):
        for guild_id in tuple(self._coordinators):
            await self.discard(guild_id)


class GuildAudioOutput(Protocol):
    """Discord adapter for the sources controlled by Guild Audio Coordination."""

    async def ensure_connected(self, channel: object) -> None: ...

    async def start_playback(
        self,
        track: Track,
        volume: float,
        after: Callable[[Optional[Exception]], None],
        start_at: float = 0.0,
    ) -> None: ...

    async def start_speech(
        self, request: TtsRequest, complete: Callable[[Optional[Exception]], None]
    ) -> None: ...

    def pause_playback(self) -> None: ...
    def resume_playback(self) -> None: ...
    def skip_playback(self) -> None: ...
    def set_volume(self, value: float) -> None: ...
    async def stop_all(self) -> None: ...
    async def disconnect(self) -> None: ...


class GuildAudioCoordinator(TtsSpeaker):
    """Owns the shared audio lifecycle for one guild."""

    def __init__(self, output: GuildAudioOutput, *, on_tts_error=None):
        self._output = output
        self._playback_active = False
        self._playback_paused = False
        self._closed = False
        self._queued_playback_starter = None
        self._tts = TtsQueue(self, on_error=on_tts_error)

    def register_queued_playback_starter(self, start) -> None:
        self._queued_playback_starter = start

    async def enqueue_speech(
        self, user_id: str, voice: str, message: str, *, bypass_cooldown: bool = False
    ) -> TtsEnqueueResult:
        result = await self._tts.enqueue(user_id, voice, message, bypass_cooldown=bypass_cooldown)
        if result.accepted and not self._playback_active and self._queued_playback_starter:
            await self._queued_playback_starter()
        return result

    async def speak(
        self, request: TtsRequest, complete: Callable[[Optional[Exception]], None]
    ) -> None:
        if self._closed:
            raise RuntimeError("Guild audio is unavailable in this voice channel")
        await self._output.start_speech(request, complete)

    async def ensure_connected(self, channel: object) -> None:
        self._ensure_open()
        await self._output.ensure_connected(channel)

    async def play(
        self,
        track: Track,
        volume: float,
        after: Callable[[Optional[Exception]], None],
        start_at: float = 0.0,
    ) -> None:
        self._ensure_open()
        # Pause is left alone: the only caller that plays while already active is a
        # seek, which restarts the same track and must not silently unpause it.
        self._playback_active = True

        def complete(error: Optional[Exception]) -> None:
            self._playback_active = False
            self._playback_paused = False
            after(error)

        try:
            await self._output.start_playback(track, volume, complete, start_at)
        except Exception:
            self._playback_active = False
            self._playback_paused = False
            raise

    def is_playing(self) -> bool:
        return self._playback_active and not self._playback_paused

    def is_paused(self) -> bool:
        return self._playback_active and self._playback_paused

    def pause(self) -> None:
        if self._playback_active and not self._playback_paused:
            self._playback_paused = True
            self._output.pause_playback()

    def resume(self) -> None:
        if self._playback_active and self._playback_paused:
            self._playback_paused = False
            self._output.resume_playback()

    def stop(self) -> None:
        if self._playback_active:
            self._output.skip_playback()

    def set_volume(self, value: float) -> None:
        self._output.set_volume(value)

    async def disconnect(self) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._playback_active = False
        self._playback_paused = False
        await self._tts.close()
        await self._output.stop_all()
        await self._output.disconnect()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Guild audio is closed")
