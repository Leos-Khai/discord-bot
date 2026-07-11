from __future__ import annotations

import asyncio
from array import array
from collections.abc import Callable
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Optional

import discord
from discord.ext import commands
import edge_tts

from db import get_database_service, get_music_channels
from logger import get_logger
from tts import TtsPreferences, TtsQueue, TtsRequest


PCM_FRAME_BYTES = 960 * 2 * 2  # 20 ms of 48 kHz, 16-bit stereo PCM
DUCKED_MUSIC_VOLUME = 0.20
FADE_FRAMES = 50  # one second at 20 ms per Discord PCM frame
VOICE_CACHE_SECONDS = 24 * 60 * 60


class TemporaryAudioSource(discord.AudioSource):
    """Owns a generated audio file and removes it when Discord finishes with it."""

    def __init__(self, source: discord.AudioSource, filename: str):
        self._source = source
        self._filename = filename
        self._cleaned = False

    @property
    def _current_error(self):
        return getattr(self._source, "_current_error", None)

    def read(self) -> bytes:
        return self._source.read()

    def is_opus(self) -> bool:
        return self._source.is_opus()

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._source.cleanup()
        Path(self._filename).unlink(missing_ok=True)


class DuckingMixer(discord.AudioSource):
    """Mixes one TTS source into a music PCM stream while smoothly ducking music."""

    def __init__(self, music: discord.AudioSource):
        self._music = music
        self._speech: Optional[TemporaryAudioSource] = None
        self._speech_complete: Optional[Callable[[Optional[Exception]], None]] = None
        self._music_factor = 1.0
        self._lock = threading.Lock()

    @property
    def _current_error(self):
        return getattr(self._music, "_current_error", None)

    def add_speech(
        self,
        source: TemporaryAudioSource,
        complete: Callable[[Optional[Exception]], None],
    ) -> None:
        with self._lock:
            if self._speech:
                raise RuntimeError("A TTS message is already being mixed")
            self._speech = source
            self._speech_complete = complete

    def set_music_volume(self, value: float) -> None:
        if isinstance(self._music, discord.PCMVolumeTransformer):
            self._music.volume = value

    def read(self) -> bytes:
        with self._lock:
            speech = self._speech
        music_frame = self._music.read()
        speech_frame = speech.read() if speech else b""

        if speech and not speech_frame:
            speech.cleanup()
            with self._lock:
                if self._speech is speech:
                    self._speech = None
                    complete = self._speech_complete
                    self._speech_complete = None
                else:
                    complete = None
            if complete:
                complete(getattr(speech, "_current_error", None))
            speech = None

        target = DUCKED_MUSIC_VOLUME if speech else 1.0
        step = (1.0 - DUCKED_MUSIC_VOLUME) / FADE_FRAMES
        if self._music_factor < target:
            self._music_factor = min(target, self._music_factor + step)
        elif self._music_factor > target:
            self._music_factor = max(target, self._music_factor - step)

        if not music_frame and not speech_frame:
            return b""
        frame_size = max(len(music_frame), len(speech_frame), PCM_FRAME_BYTES)
        music_frame = music_frame.ljust(frame_size, b"\x00")
        speech_frame = speech_frame.ljust(frame_size, b"\x00")
        return self._mix(music_frame, speech_frame, self._music_factor)

    @staticmethod
    def _mix(music: bytes, speech: bytes, music_factor: float) -> bytes:
        music_samples = array("h")
        speech_samples = array("h")
        music_samples.frombytes(music)
        speech_samples.frombytes(speech)
        for index, music_sample in enumerate(music_samples):
            value = int(music_sample * music_factor) + speech_samples[index]
            music_samples[index] = max(-32768, min(32767, value))
        return music_samples.tobytes()

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        self._music.cleanup()
        with self._lock:
            speech = self._speech
            self._speech = None
            complete = self._speech_complete
            self._speech_complete = None
        if speech:
            speech.cleanup()
        if complete:
            complete(RuntimeError("TTS playback stopped"))


class EdgeTtsSynthesizer:
    async def synthesize(self, text: str, voice: str) -> str:
        descriptor, filename = tempfile.mkstemp(prefix="discord-tts-", suffix=".mp3")
        os.close(descriptor)
        try:
            await edge_tts.Communicate(text, voice).save(filename)
        except Exception:
            Path(filename).unlink(missing_ok=True)
            raise
        return filename

    async def list_voices(self) -> list[dict]:
        return await edge_tts.list_voices()


class GuildTtsSpeaker:
    """Discord adapter that either starts TTS or inserts it into active music."""

    def __init__(self, guild: discord.Guild, synthesizer: EdgeTtsSynthesizer):
        self._guild = guild
        self._synthesizer = synthesizer

    async def speak(
        self, request: TtsRequest, complete: Callable[[Optional[Exception]], None]
    ) -> None:
        filename = await self._synthesizer.synthesize(request.message, request.voice)
        source = TemporaryAudioSource(discord.FFmpegPCMAudio(filename), filename)
        client = self._guild.voice_client
        if not client or not client.is_connected():
            source.cleanup()
            raise RuntimeError("The bot is no longer connected to a voice channel")
        if client.is_paused():
            source.cleanup()
            raise RuntimeError("Resume music before using TTS")
        if client.is_playing():
            active_source = client.source
            if isinstance(active_source, DuckingMixer):
                mixer = active_source
            else:
                mixer = DuckingMixer(active_source)
                client.source = mixer
            mixer.add_speech(source, complete)
            return

        def after(error: Optional[Exception]) -> None:
            complete(error or getattr(source, "_current_error", None))

        client.play(source, after=after)


class VoiceListView(discord.ui.View):
    PAGE_SIZE = 20

    def __init__(self, voices: list[dict]):
        super().__init__(timeout=120)
        self._voices = voices
        self._page = 0

    def _embed(self) -> discord.Embed:
        start = self._page * self.PAGE_SIZE
        page = self._voices[start : start + self.PAGE_SIZE]
        lines = [
            f"`{voice['ShortName']}` — {voice.get('Locale', 'Unknown')} ({voice.get('Gender', 'Unknown')})"
            for voice in page
        ]
        pages = max(1, (len(self._voices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        embed = discord.Embed(
            title="Available TTS voices",
            description="\n".join(lines) or "No voices match that language.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Page {self._page + 1} of {pages} • Use !setttsvoice <voice ID>")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        pages = max(1, (len(self._voices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._page = (self._page - 1) % pages
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        pages = max(1, (len(self._voices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._page = (self._page + 1) % pages
        await interaction.response.edit_message(embed=self._embed(), view=self)


class TtsCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = get_logger()
        self.preferences = TtsPreferences(get_database_service())
        self.synthesizer = EdgeTtsSynthesizer()
        self.queues: dict[str, TtsQueue] = {}
        self.speakers: dict[str, GuildTtsSpeaker] = {}
        self._voice_cache: list[dict] = []
        self._voices_cached_at = 0.0
        self._voice_lock = asyncio.Lock()

    async def cog_check(self, ctx):
        message = await self._channel_error(ctx.guild, ctx.channel.id)
        if message:
            await ctx.send(message)
            return False
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        message = await self._channel_error(interaction.guild, interaction.channel_id)
        if not message:
            return True
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def _channel_error(
        self, guild: Optional[discord.Guild], channel_id: Optional[int]
    ) -> Optional[str]:
        if not guild:
            return "TTS commands are only available in a server."
        allowed = [int(channel) for channel in await get_music_channels(str(guild.id))]
        if not allowed:
            return "An admin must configure a music command channel before TTS can be used."
        if channel_id not in allowed:
            return "TTS commands are limited to: " + ", ".join(f"<#{channel}>" for channel in allowed)
        return None

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            return
        raise error

    async def _voices(self) -> list[dict]:
        now = time.monotonic()
        if self._voice_cache and now - self._voices_cached_at < VOICE_CACHE_SECONDS:
            return self._voice_cache
        async with self._voice_lock:
            now = time.monotonic()
            if not self._voice_cache or now - self._voices_cached_at >= VOICE_CACHE_SECONDS:
                self._voice_cache = sorted(
                    await self.synthesizer.list_voices(), key=lambda voice: voice["ShortName"].casefold()
                )
                self._voices_cached_at = now
        return self._voice_cache

    async def _ensure_voice_channel(self, ctx) -> bool:
        voice_state = getattr(ctx.author, "voice", None)
        channel = voice_state.channel if voice_state else None
        if not channel:
            await ctx.send("You are not connected to a voice channel.")
            return False
        client = ctx.guild.voice_client
        if client and client.is_connected() and client.channel != channel:
            await ctx.send("I am already being used in a different voice channel.")
            return False
        if not client:
            try:
                await channel.connect()
            except discord.DiscordException as error:
                self.logger.warning("Unable to join voice channel %s: %s", channel.id, error)
                await ctx.send(f"I could not join your voice channel: {error}")
                return False
        return True

    def _queue(self, guild: discord.Guild) -> TtsQueue:
        guild_id = str(guild.id)
        if guild_id not in self.queues:
            speaker = self.speakers.setdefault(guild_id, GuildTtsSpeaker(guild, self.synthesizer))
            self.queues[guild_id] = TtsQueue(
                speaker,
                on_error=lambda error: self.logger.warning("TTS playback failed: %s", error),
                on_idle=lambda: self._start_queued_music(guild_id),
            )
        return self.queues[guild_id]

    async def _start_queued_music(self, guild_id: str) -> None:
        music = self.bot.get_cog("MusicCommands")
        if music:
            await music.start_queued_if_idle(guild_id)

    @commands.hybrid_command(name="tts", help="Speak a message in your voice channel.")
    async def tts(self, ctx, *, message: str):
        if not await self._ensure_voice_channel(ctx):
            return
        guild_id, user_id = str(ctx.guild.id), str(ctx.author.id)
        voice = await self.preferences.voice_for(guild_id, user_id)
        result = await self._queue(ctx.guild).enqueue(
            user_id,
            voice,
            message,
            bypass_cooldown=ctx.author.guild_permissions.administrator,
        )
        if not result.accepted:
            await ctx.send(f"TTS request rejected: {result.detail}")
        elif result.queued:
            await ctx.send("TTS message queued.")
        else:
            await ctx.send("Speaking your message.")

    @commands.hybrid_command(name="setttsvoice", help="Set your TTS voice for this server.")
    async def setttsvoice(self, ctx, *, voice: str):
        try:
            voices = await self._voices()
        except Exception as error:
            self.logger.warning("Unable to fetch Edge TTS voices: %s", error)
            await ctx.send("I could not fetch the TTS voice catalogue. Please try again later.")
            return
        matches = {item["ShortName"].casefold(): item["ShortName"] for item in voices}
        selected = matches.get(voice.casefold())
        if not selected:
            suggestions = [item["ShortName"] for item in voices if voice.casefold() in item["ShortName"].casefold()][:5]
            suffix = f" Try: {', '.join(f'`{item}`' for item in suggestions)}" if suggestions else ""
            await ctx.send(f"Unknown voice `{voice}`.{suffix}")
            return
        await self.preferences.set_voice(str(ctx.guild.id), str(ctx.author.id), selected)
        await ctx.send(f"Your TTS voice for this server is now `{selected}`.")

    @commands.hybrid_command(name="listvoice", help="List voices available for TTS.")
    async def listvoice(self, ctx, language: Optional[str] = None):
        try:
            voices = await self._voices()
        except Exception as error:
            self.logger.warning("Unable to fetch Edge TTS voices: %s", error)
            await ctx.send("I could not fetch the TTS voice catalogue. Please try again later.")
            return
        if language:
            language = language.casefold()
            voices = [
                voice
                for voice in voices
                if voice.get("Locale", "").casefold() == language
                or voice.get("Locale", "").casefold().startswith(f"{language}-")
            ]
        view = VoiceListView(voices)
        await ctx.send(embed=view._embed(), view=view)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not member.guild:
            return
        guild_id = str(member.guild.id)
        if self.bot.user and member.id == self.bot.user.id and before.channel and not after.channel:
            queue = self.queues.pop(guild_id, None)
            self.speakers.pop(guild_id, None)
            if queue:
                await queue.close()
            return
        client = member.guild.voice_client
        if not client or not client.channel:
            return
        if any(not channel_member.bot for channel_member in client.channel.members):
            return
        queue = self.queues.pop(guild_id, None)
        self.speakers.pop(guild_id, None)
        if queue:
            await queue.close()
        await client.disconnect()

    def cog_unload(self):
        for queue in self.queues.values():
            self.bot.loop.create_task(queue.close())


async def setup(bot):
    await bot.add_cog(TtsCommands(bot))
