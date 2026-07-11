import asyncio
import functools
import re
from typing import Optional

import discord
from discord.ext import commands
import yt_dlp as youtube_dl

from db import get_music_channels, get_playback_volume, set_playback_volume
from guild_playback import GuildPlayback, OutcomeKind, PlaybackOutcome, Track, TrackRequest
from logger import get_logger

youtube_dl.utils.bug_reports_message = lambda *args, **kwargs: ""

YDL_OPTIONS = {
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "quiet": True,
    "forceipv4": True,
    "ignoreerrors": True,
    "nocheckcertificate": True,
    "extractor_retries": 3,
    "skip_download": True,
    "no_warnings": True,
    "youtube_include_dash_manifest": False,
    "prefer_free_formats": True,
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/91.0.4472.124 Safari/537.36",
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1",
    "options": "-vn -bufsize 512k -maxrate 128k",
}


class YtdlpMediaAdapter:
    def __init__(self, logger):
        self.logger = logger

    async def _extract(self, target: str, *, flat: bool, playlist: bool, format_option=None):
        options = YDL_OPTIONS.copy()
        options.update({"extract_flat": flat, "noplaylist": not playlist, "socket_timeout": 30})
        if format_option:
            options["format"] = format_option
        ydl = youtube_dl.YoutubeDL(options)
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, functools.partial(ydl.extract_info, target, download=False)),
            timeout=60.0 if playlist else 45.0,
        )

    async def resolve(self, request: TrackRequest) -> Track:
        data = None
        last_error = None
        for format_option in ("bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio", "best[height<=720]/best", "worst"):
            try:
                data = await self._extract(request.target, flat=False, playlist=False, format_option=format_option)
                if data:
                    break
            except Exception as error:
                last_error = error
                self.logger.warning("Failed to resolve '%s' with %s: %s", request.target, format_option, error)
        if not data:
            raise RuntimeError("Could not find a playable audio source") from last_error
        if data.get("entries"):
            entry = next((item for item in data["entries"] if item), None)
            if not entry:
                raise RuntimeError("Could not find a playable audio source")
            target = entry.get("webpage_url") or entry.get("url") or entry.get("id")
            if not target:
                raise RuntimeError("Could not find a playable audio source")
            return await self.resolve(TrackRequest(target))
        stream_url = data.get("url")
        if not stream_url or stream_url.startswith("file://"):
            raise RuntimeError("Could not find a playable audio source")
        return Track(
            title=data.get("title") or request.title or "Unknown Title",
            stream_url=stream_url,
            webpage_url=data.get("webpage_url") or request.target,
            duration=data.get("duration") or request.duration,
            uploader=data.get("uploader"),
            thumbnail=data.get("thumbnail"),
        )

    async def playlist_entries(self, request: TrackRequest) -> list[TrackRequest]:
        match = re.search(r"[?&]list=([A-Za-z0-9_-]+)", request.target)
        target = f"https://www.youtube.com/playlist?list={match.group(1)}" if match else request.target
        data = await self._extract(target, flat=True, playlist=True)
        entries = data.get("entries", []) if data else []
        return [
            TrackRequest(
                entry.get("webpage_url") or entry.get("url") or entry.get("id"),
                entry.get("title") or "Unknown Title",
                entry.get("duration"),
            )
            for entry in entries[:250]
            if entry and (entry.get("webpage_url") or entry.get("url") or entry.get("id"))
        ]

    async def search(self, query: str) -> list[TrackRequest]:
        data = await self._extract(f"ytsearch5:{query}", flat=True, playlist=False)
        return [
            TrackRequest(
                entry.get("webpage_url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
                entry.get("title") or "Unknown Title",
                entry.get("duration"),
            )
            for entry in (data or {}).get("entries", [])[:5]
            if entry
        ]


class DiscordVoiceAdapter:
    def __init__(self, guild: discord.Guild):
        self.guild = guild

    async def ensure_connected(self, channel: discord.VoiceChannel) -> None:
        client = self.guild.voice_client
        if client is None:
            await channel.connect()
        elif client.channel != channel:
            await client.move_to(channel)

    async def play(self, track: Track, volume: float, after) -> None:
        client = self.guild.voice_client
        if not client or not client.is_connected():
            raise RuntimeError("Voice client unavailable for playback")
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTIONS), volume=volume
        )
        client.play(source, after=after)

    def is_playing(self) -> bool:
        return bool(self.guild.voice_client and self.guild.voice_client.is_playing())

    def is_paused(self) -> bool:
        return bool(self.guild.voice_client and self.guild.voice_client.is_paused())

    def pause(self) -> None:
        if self.guild.voice_client:
            self.guild.voice_client.pause()

    def resume(self) -> None:
        if self.guild.voice_client:
            self.guild.voice_client.resume()

    def stop(self) -> None:
        if self.guild.voice_client:
            self.guild.voice_client.stop()

    async def disconnect(self) -> None:
        if self.guild.voice_client:
            await self.guild.voice_client.disconnect()

    def set_volume(self, value: float) -> None:
        source = self.guild.voice_client.source if self.guild.voice_client else None
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = value


class MongoVolumeAdapter:
    async def load(self, guild_id: str) -> float:
        return await get_playback_volume(guild_id)

    async def save(self, guild_id: str, value: float) -> None:
        await set_playback_volume(guild_id, value)


class DiscordOutcomeAdapter:
    def __init__(self):
        self.channel: Optional[discord.abc.Messageable] = None

    def use_channel(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel

    async def publish(self, outcome: PlaybackOutcome) -> None:
        if not self.channel:
            return
        if outcome.kind == OutcomeKind.STARTED and outcome.track:
            await self.channel.send(f"Now playing: **{outcome.track.title}**")
        elif outcome.kind == OutcomeKind.PLAYLIST_LOADING:
            await self.channel.send(f"Loading {outcome.detail} playlist track(s) in the background…")
        elif outcome.kind == OutcomeKind.PLAYLIST_COMPLETE:
            await self.channel.send("Playlist loading complete.")
        elif outcome.kind == OutcomeKind.FAILED and outcome.detail:
            await self.channel.send(f"Playback issue: {outcome.detail}")


class MusicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = get_logger()
        self.media = YtdlpMediaAdapter(self.logger)
        self.playbacks: dict[str, GuildPlayback] = {}
        self.outcomes: dict[str, DiscordOutcomeAdapter] = {}
        self.allowed_channels_cache: dict[str, set[int]] = {}

    async def _get_allowed_channels(self, guild_id: str) -> set[int]:
        if guild_id not in self.allowed_channels_cache:
            self.allowed_channels_cache[guild_id] = {int(channel) for channel in await get_music_channels(guild_id)}
        return self.allowed_channels_cache[guild_id]

    async def refresh_allowed_channels_cache(self, guild_id: str):
        self.allowed_channels_cache.pop(guild_id, None)
        await self._get_allowed_channels(guild_id)

    async def cog_check(self, ctx):
        if not ctx.guild:
            return False
        allowed = await self._get_allowed_channels(str(ctx.guild.id))
        if allowed and ctx.channel.id not in allowed:
            await ctx.send("Music commands are limited to: " + ", ".join(f"<#{channel}>" for channel in allowed))
            return False
        return True

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            return
        raise error

    def _playback(self, ctx) -> GuildPlayback:
        guild_id = str(ctx.guild.id)
        publisher = self.outcomes.setdefault(guild_id, DiscordOutcomeAdapter())
        publisher.use_channel(ctx.channel)
        if guild_id not in self.playbacks:
            self.playbacks[guild_id] = GuildPlayback(
                guild_id,
                DiscordVoiceAdapter(ctx.guild),
                self.media,
                MongoVolumeAdapter(),
                publisher,
                self.logger,
            )
        return self.playbacks[guild_id]

    async def _voice_channel(self, ctx):
        if not ctx.author.voice:
            await ctx.send("You are not connected to a voice channel.")
            return None
        return ctx.author.voice.channel

    @staticmethod
    def _format_duration(seconds: Optional[float]) -> str:
        if seconds is None:
            return "N/A"
        minutes, seconds = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"

    async def _send_enqueue_outcome(self, ctx, outcome: PlaybackOutcome):
        if outcome.kind == OutcomeKind.STARTED and outcome.track:
            await ctx.send(f"Now playing: **{outcome.track.title}**")
        elif outcome.kind == OutcomeKind.QUEUED and outcome.track:
            await ctx.send(f"Added to queue: **{outcome.track.title}**")
        elif outcome.kind == OutcomeKind.FAILED:
            await ctx.send(f"Unable to play: {outcome.detail}")

    @commands.hybrid_command(help="Have the bot join your current voice channel.")
    async def join(self, ctx):
        channel = await self._voice_channel(ctx)
        if channel:
            await self._playback(ctx).join(channel)
            await ctx.send(f"Connected to **{channel.name}**.")

    @commands.hybrid_command(help="Play a YouTube URL, playlist, or search query.")
    async def play(self, ctx, *, query: str):
        if not query:
            await ctx.send("Please provide a URL or search terms to play.")
            return
        channel = await self._voice_channel(ctx)
        if not channel:
            return
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()
        playback = self._playback(ctx)
        target = query.strip()
        is_playlist = bool(re.match(r"https?://", target) and re.search(r"[?&]list=", target))
        outcome = await (
            playback.enqueue_playlist(TrackRequest(target), channel)
            if is_playlist
            else playback.enqueue(TrackRequest(target), channel)
        )
        await self._send_enqueue_outcome(ctx, outcome)

    @commands.hybrid_command(help="Search YouTube and choose a result.")
    async def search(self, ctx, *, query: str):
        channel = await self._voice_channel(ctx)
        if not channel:
            return
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer()
        await ctx.send(f"Searching YouTube for '{query}'...")
        try:
            entries = await self.media.search(query)
        except Exception as error:
            self.logger.warning("Search failed for '%s': %s", query, error)
            await ctx.send(f"Search failed: {error}")
            return
        if not entries:
            await ctx.send("No results found.")
            return
        lines = [f"{i}. {entry.title} ({self._format_duration(entry.duration)})" for i, entry in enumerate(entries, 1)]
        embed = discord.Embed(title="Search Results", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text="React with 1-5 to choose. Expires in 30s.")
        message = await ctx.send(embed=embed)
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"][: len(entries)]
        for emoji in emojis:
            try:
                await message.add_reaction(emoji)
            except discord.Forbidden:
                await ctx.send("I need permission to add reactions in this channel.")
                return

        def selected(reaction, user):
            return user == ctx.author and reaction.message.id == message.id and str(reaction.emoji) in emojis

        try:
            reaction, user = await self.bot.wait_for("reaction_add", timeout=30.0, check=selected)
        except asyncio.TimeoutError:
            await ctx.send("Selection timed out.")
            return
        request = entries[emojis.index(str(reaction.emoji))]
        outcome = await self._playback(ctx).enqueue(request, channel)
        await self._send_enqueue_outcome(ctx, outcome)

    @commands.hybrid_command(name="queue", help="Display the current queue.")
    async def queue_list(self, ctx):
        playback = self._playback(ctx)
        current = playback.current_track
        queued = playback.queued_tracks
        if not current and not queued and not playback.is_loading:
            await ctx.send("The queue is empty and nothing is loading.")
            return
        lines = []
        if current:
            lines.append(f"Now playing: **{current.title}**")
        lines.extend(f"{i}. {track.title}" for i, track in enumerate(queued[:10], 1))
        if playback.is_loading:
            lines.append("More playlist tracks are loading…")
        await ctx.send("\n".join(lines))

    @commands.hybrid_command(help="Show the currently playing track.")
    async def np(self, ctx):
        current = self._playback(ctx).current_track
        if not current:
            await ctx.send("No track is currently playing.")
            return
        position = self._playback(ctx).position()
        suffix = f" — {self._format_duration(position)}" if position is not None else ""
        await ctx.send(f"Now playing: **{current.title}**{suffix}")

    @commands.hybrid_command(help="Show source details for the current track.")
    async def source(self, ctx):
        current = self._playback(ctx).current_track
        if not current:
            await ctx.send("No track is currently playing.")
            return
        embed = discord.Embed(title=current.title, url=current.webpage_url or discord.Embed.Empty, color=discord.Color.blurple())
        if current.uploader:
            embed.add_field(name="Channel", value=current.uploader)
        if current.duration is not None:
            embed.add_field(name="Duration", value=self._format_duration(current.duration))
        if current.thumbnail:
            embed.set_thumbnail(url=current.thumbnail)
        await ctx.send(embed=embed)

    @commands.hybrid_command(help="Skip the current track.")
    async def skip(self, ctx):
        outcome = await self._playback(ctx).skip()
        await ctx.send("Track skipped." if outcome.kind == OutcomeKind.SKIPPED else outcome.detail)

    @commands.hybrid_command(help="Remove tracks from the queue.")
    async def remove(self, ctx, arg: str):
        outcome = await self._playback(ctx).remove(arg)
        if outcome.kind == OutcomeKind.REMOVED and outcome.track:
            await ctx.send(f"Removed track: **{outcome.track.title}**")
        elif outcome.kind == OutcomeKind.CLEARED:
            await ctx.send(f"Cleared the queue. Removed {outcome.detail} track(s).")
        else:
            await ctx.send(outcome.detail)

    @commands.hybrid_command(help="Set the guild playback volume (0-150).")
    async def volume(self, ctx, vol: Optional[int] = None):
        playback = self._playback(ctx)
        if vol is None:
            await ctx.send(f"Current volume is **{int(await playback.volume() * 100)}%**.")
            return
        outcome = await playback.set_volume(vol / 100)
        await ctx.send(f"Volume set to **{vol}%**." if outcome.kind == OutcomeKind.VOLUME_CHANGED else f"Unable to set volume: {outcome.detail}")

    @commands.hybrid_command(help="Stop playback and disconnect the bot.")
    async def stop(self, ctx):
        guild_id = str(ctx.guild.id)
        playback = self.playbacks.pop(guild_id, None)
        self.outcomes.pop(guild_id, None)
        if not playback:
            await ctx.send("Nothing is playing.")
            return
        await playback.close()
        await ctx.send("Stopped playback and disconnected.")

    @commands.hybrid_command(help="Pause playback.")
    async def pause(self, ctx):
        outcome = await self._playback(ctx).pause()
        await ctx.send("Music paused." if outcome.kind == OutcomeKind.PAUSED else outcome.detail)

    @commands.hybrid_command(help="Resume playback.")
    async def resume(self, ctx):
        outcome = await self._playback(ctx).resume()
        await ctx.send("Music resumed." if outcome.kind == OutcomeKind.RESUMED else outcome.detail)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not self.bot.user or member.id != self.bot.user.id or not before.channel or after.channel:
            return
        guild_id = str(member.guild.id)
        playback = self.playbacks.pop(guild_id, None)
        self.outcomes.pop(guild_id, None)
        if playback:
            await playback.close(disconnect=False)

    def cog_unload(self):
        for playback in self.playbacks.values():
            self.bot.loop.create_task(playback.close())


async def setup(bot):
    await bot.add_cog(MusicCommands(bot))
