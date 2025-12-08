import discord
from discord.ext import commands
import re
import datetime
import yt_dlp as youtube_dl
import os
import json
from cogs.admin import is_admin
import functools
from logger import get_logger
import asyncio
from db import get_music_channels

youtube_dl.utils.bug_reports_message = lambda *args, **kwargs: ""

# Updated YDL_OPTIONS with better format selection and error handling
YDL_OPTIONS = {
    "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": False,
    "quiet": True,
    "force-ipv4": True,
    "extract_flat": "in_playlist",
    "ignoreerrors": True,
    "nocheckcertificate": True,
    "extractor_retries": 3,
    "skip_download": True,
    "no_warnings": True,
    # Remove complex format_sort that might be causing issues
    # YouTube specific options - simplified
    "youtube_include_dash_manifest": False,
    "prefer_free_formats": True,
    # Add cookies support if needed
    "cookiefile": None,
    # Add user agent
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
}

# Updated FFMPEG options with better reconnect handling
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1",
    "options": "-vn -bufsize 512k -maxrate 128k",
}


class MusicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = get_logger()
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.volumes_file = os.path.join(script_dir, "..", "volumes.json")
        if os.path.exists(self.volumes_file):
            try:
                with open(self.volumes_file, "r") as f:
                    self.volumes = json.load(f)
            except Exception as e:
                self.logger.error(f"Error loading volumes file: {e}")
                self.volumes = {}
        else:
            self.volumes = {}
        self.queues = {}
        self.current_tracks = {}
        self.loading_queues = {}
        self.loading_tasks = {}
        self.loading_locks = {}
        self.play_next_events = {}
        self.is_seeking = {}
        self.playback_start_time = {}
        self.playback_seek_position = {}
        self.pause_start_time = {}
        self.allowed_channels_cache = {}

    async def _get_allowed_channels(self, guild_id: str):
        """Fetch allowed text channels for music commands, cached per guild."""
        if guild_id in self.allowed_channels_cache:
            return self.allowed_channels_cache[guild_id]
        channels = await get_music_channels(guild_id)
        allowed_set = {int(cid) for cid in channels}
        self.allowed_channels_cache[guild_id] = allowed_set
        return allowed_set

    async def refresh_allowed_channels_cache(self, guild_id: str):
        """Refresh allowed channel cache after admin updates."""
        if guild_id in self.allowed_channels_cache:
            self.allowed_channels_cache.pop(guild_id, None)
        await self._get_allowed_channels(guild_id)

    async def cog_check(self, ctx):
        """Restrict music commands to allowed channels if configured."""
        if not ctx.guild:
            return False
        allowed_channels = await self._get_allowed_channels(str(ctx.guild.id))
        if allowed_channels and ctx.channel.id not in allowed_channels:
            mentions = ", ".join(f"<#{cid}>" for cid in allowed_channels)
            await ctx.send(f"Music commands are limited to: {mentions}")
            return False
        return True

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            return
        raise error

    def get_guild_queue(self, gid):
        if gid not in self.queues:
            self.queues[gid] = []
        if gid not in self.loading_queues:
            self.loading_queues[gid] = []
        if gid not in self.loading_locks:
            self.loading_locks[gid] = asyncio.Lock()
        if gid not in self.play_next_events:
            self.play_next_events[gid] = asyncio.Event()
        return self.queues[gid]

    def _format_duration(self, seconds: float) -> str:
        """Formats seconds into HH:MM:SS or MM:SS string."""
        if seconds is None:
            return "N/A"
        try:
            seconds = int(float(seconds))
        except (ValueError, TypeError):
            return "N/A"
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"

    def _get_current_position(self, gid: str) -> float | None:
        """Estimates the current playback position in seconds."""
        loop = self.bot.loop
        start_time = self.playback_start_time.get(gid)
        seek_pos = self.playback_seek_position.get(gid, 0.0)
        pause_start = self.pause_start_time.get(gid)

        if start_time is None:
            return None

        current_time = loop.time()
        elapsed_time = current_time - start_time

        if pause_start:
            time_paused = current_time - pause_start
            elapsed_time -= time_paused

        elapsed_time = max(0.0, elapsed_time)

        return seek_pos + elapsed_time

    async def _start_track(self, ctx, gid: str, track: dict, announce: bool = True):
        """Start playback for a prepared track and update state."""
        vc = ctx.guild.voice_client
        if not vc or not vc.is_connected():
            raise RuntimeError("Voice client unavailable for playback.")

        if not track or "url" not in track:
            raise ValueError("Track is missing stream URL.")

        volume = self.volumes.get(gid, 1.0)
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTIONS),
            volume=volume,
        )

        def _after(error_arg):
            self.handle_after_play(error_arg, ctx, gid)

        vc.play(source, after=_after)

        loop = self.bot.loop
        self.playback_start_time[gid] = loop.time()
        self.playback_seek_position[gid] = 0.0
        self.pause_start_time.pop(gid, None)
        self.current_tracks[gid] = track

        if announce and ctx and ctx.channel:
            await ctx.send(f"Now playing: **{track.get('title', 'Unknown')}**")

    async def _fetch_track_info(self, url_or_id):
        """Fetches full track info for a single URL or ID. Runs in executor."""
        loop = self.bot.loop

        # Simplified format selection - try these in order
        formats_to_try = [
            "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
            "best[height<=720]/best",
            "worst",  # Last resort
        ]

        for format_option in formats_to_try:
            try:
                ydl_opts = YDL_OPTIONS.copy()
                ydl_opts["format"] = format_option
                ydl_opts["noplaylist"] = True
                # Force full extraction so we can resolve stream URLs from searches/playlists
                ydl_opts["extract_flat"] = False

                # Add timeout to prevent hanging
                ydl_opts["socket_timeout"] = 30

                ydl = youtube_dl.YoutubeDL(ydl_opts)

                # Add timeout to the executor call
                try:
                    data = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            functools.partial(
                                ydl.extract_info, url_or_id, download=False
                            ),
                        ),
                        timeout=45.0,  # 45 second timeout
                    )
                except asyncio.TimeoutError:
                    self.logger.warning(f"Timeout fetching track info for {url_or_id}")
                    continue

                if not data:
                    continue

                # If we got a search/playlist result, dig into the first entry and resolve it
                if data.get("entries"):
                    first_entry = next((entry for entry in data["entries"] if entry), None)
                    if not first_entry:
                        continue
                    entry_target = (
                        first_entry.get("webpage_url")
                        or first_entry.get("url")
                        or first_entry.get("id")
                    )
                    if entry_target:
                        return await self._fetch_track_info(entry_target)
                    continue

                stream_url = data.get("url")
                if stream_url and not stream_url.startswith("file://"):
                    return data

            except Exception as e:
                self.logger.warning(
                    f"Failed to fetch track with format {format_option}: {str(e)}"
                )
                continue

        raise Exception("Could not extract audio information with any available format")

    async def play_next(self, ctx, gid):
        """Plays the next track in the queue or waits for the background loader."""
        queue = self.get_guild_queue(gid)
        play_next_event = self.play_next_events.get(gid)

        if queue:
            volume = self.volumes.get(gid, 1.0)
            next_track = queue.pop(0)

            vc = ctx.guild.voice_client
            if not vc or not vc.is_connected():
                self.logger.warning(
                    f"play_next called for GID {gid} but voice client is disconnected."
                )
                self.current_tracks[gid] = None
                return

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    # Refresh the URL to prevent expiration issues
                    if "webpage_url" in next_track:
                        fresh_track = await self._fetch_track_info(
                            next_track["webpage_url"]
                        )
                        if fresh_track and "url" in fresh_track:
                            next_track["url"] = fresh_track["url"]
                    await self._start_track(ctx, gid, next_track, announce=True)
                    break  # Success, exit retry loop

                except Exception as e:
                    self.logger.warning(f"Attempt {attempt + 1} failed: {str(e)}")
                    if attempt == max_retries - 1:  # Last attempt
                        if ctx and ctx.channel:
                            await ctx.send(
                                f"❌ Failed to play: **{next_track.get('title', 'Unknown')}** - Skipping..."
                            )
                        self.logger.error(
                            f"Error in play_next starting track {next_track.get('title', 'N/A')}: {e}"
                        )
                        # Try next track
                        self.bot.loop.create_task(self.play_next(ctx, gid))
                    else:
                        await asyncio.sleep(1)  # Wait before retry

        else:
            loader_task = self.loading_tasks.get(gid)
            if loader_task and not loader_task.done():
                self.logger.info(
                    f"play_next for GID {gid}: Queue empty, waiting for background loader..."
                )
                try:
                    await asyncio.wait_for(play_next_event.wait(), timeout=10.0)
                    play_next_event.clear()
                    self.logger.info(
                        f"play_next for GID {gid}: Loader added track, retrying..."
                    )
                    self.bot.loop.create_task(self.play_next(ctx, gid))
                except asyncio.TimeoutError:
                    self.logger.info(
                        f"play_next for GID {gid}: Timed out waiting for loader. Queue finished."
                    )
                    self.current_tracks[gid] = None
                    if ctx and ctx.channel:
                        await ctx.send("Queue finished.")
                except Exception as e:
                    self.logger.error(
                        f"Error waiting for play_next_event for GID {gid}: {e}"
                    )
                    self.current_tracks[gid] = None
            else:
                self.logger.info(
                    f"play_next for GID {gid}: Queue empty and no active loader. Playback finished."
                )
                self.current_tracks[gid] = None

    def handle_after_play(self, error, ctx, gid):
        """Callback function for after a track finishes playing or errors."""
        if self.is_seeking.pop(gid, None):
            self.logger.debug(f"handle_after_play skipped for GID {gid} due to seek.")
            return
        if error:
            self.logger.error(f"Error after playing track for GID {gid}: {error}")

        self.bot.loop.create_task(self.play_next(ctx, gid))

    async def _ensure_voice(self, ctx):
        """Checks if the user is in a voice channel and connects/moves the bot."""
        if not ctx.author.voice:
            await ctx.send("You are not connected to a voice channel.")
            return False

        channel = ctx.author.voice.channel
        if ctx.voice_client is None:
            try:
                await channel.connect()
                await ctx.send(f"Joined **{channel.name}**!")
            except Exception as e:
                await ctx.send(f"Error connecting to voice channel: {e}")
                self.logger.error(f"Error connecting to voice channel: {e}")
                return False
        elif ctx.voice_client.channel != channel:
            try:
                await ctx.voice_client.move_to(channel)
                await ctx.send(f"Moved to **{channel.name}**!")
            except Exception as e:
                await ctx.send(f"Error moving voice channel: {e}")
                self.logger.error(f"Error moving voice channel: {e}")
                return False
        return True

    @commands.command(
        help="Have the bot join your current voice channel without starting playback.\nUsage: !join"
    )
    async def join(self, ctx):
        if not await self._ensure_voice(ctx):
            return
        # If we were already in the user's channel, give a quick confirmation.
        if (
            ctx.voice_client
            and ctx.author.voice
            and ctx.voice_client.channel == ctx.author.voice.channel
        ):
            await ctx.send(f"Already in **{ctx.voice_client.channel.name}**.")

    @commands.command(
        help="Play a YouTube URL or search query.\nUsage: !play <url|search terms>\nAutomatically joins your voice channel."
    )
    async def play(self, ctx, *, query: str):
        if not query:
            await ctx.send("Please provide a URL or search terms to play.")
            return

        if not await self._ensure_voice(ctx):
            return

        gid = str(ctx.guild.id)
        queue = self.get_guild_queue(gid)

        target = query.strip()
        is_url = re.match(r"https?://", target)
        ydl_target = target if is_url else f"ytsearch1:{target}"

        try:
            track_info = await self._fetch_track_info(ydl_target)
        except Exception as e:
            await ctx.send(f"❌ Unable to fetch track: {e}")
            self.logger.error(
                f"Failed to fetch track for '{query}' in GID {gid}: {e}"
            )
            return

        if not track_info or "url" not in track_info:
            await ctx.send("❌ Couldn't find a playable audio source for that request.")
            return

        track = {
            "title": track_info.get("title") or "Unknown Title",
            "url": track_info.get("url"),
            "duration": track_info.get("duration"),
            "webpage_url": track_info.get("webpage_url") or track_info.get("url"),
        }

        if ctx.voice_client and ctx.voice_client.is_playing():
            queue.append(track)
            await ctx.send(f"➕ Added to queue: **{track['title']}**")
        else:
            try:
                await self._start_track(ctx, gid, track, announce=False)
                await ctx.send(f"▶️ Now playing: **{track['title']}**")
            except Exception as e:
                await ctx.send(f"❌ Error starting playback: {e}")
                self.logger.error(
                    f"Error starting playback for {track.get('title', 'N/A')} in GID {gid}: {e}"
                )

    @commands.command(
        help="Search YouTube and choose a song to play.\nUsage: !search <query>\nShows top 5 results and lets you choose.\nExample: !search never gonna give you up"
    )
    async def search(self, ctx, *, query: str):
        if not ctx.voice_client:
            if ctx.author.voice:
                if not await self._ensure_voice(ctx):
                    return
            else:
                await ctx.send("You must be in a voice channel to play audio.")
                return
        elif not await self._ensure_voice(ctx):
            return

        gid = str(ctx.guild.id)
        loop = self.bot.loop
        await ctx.send(f"🔍 Searching YouTube for '{query}'...")

        try:
            ydl_opts_search = YDL_OPTIONS.copy()
            ydl_opts_search["noplaylist"] = True
            ydl_opts_search["socket_timeout"] = 30
            ydl = youtube_dl.YoutubeDL(ydl_opts_search)

            info = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    functools.partial(
                        ydl.extract_info, f"ytsearch5:{query}", download=False
                    ),
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            await ctx.send(f"❌ Search timed out for '{query}'.")
            return
        except Exception as e:
            await ctx.send(f"❌ Error processing search: {e}")
            self.logger.error(f"Error processing search '{query}': {e}")
            return

        entries = info.get("entries", [])
        if not entries:
            await ctx.send("? No results found.")
            return

        entries = entries[:5]
        lines = []
        for i, entry in enumerate(entries, start=1):
            duration = entry.get("duration")
            duration_str = f" ({self._format_duration(duration)})" if duration else ""
            lines.append(f"{i}. {entry.get('title', 'Unknown Title')}{duration_str}")

        embed = discord.Embed(
            title="Search Results",
            description="\n".join(lines),
            color=0x5865F2,
        )
        embed.set_footer(text="React with 1-5 to choose. Expires in 30s.")
        msg = await ctx.send(embed=embed)

        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        available_emojis = number_emojis[: len(entries)]
        for emoji in available_emojis:
            try:
                await msg.add_reaction(emoji)
            except discord.Forbidden:
                await ctx.send("❌ I need permission to add reactions in this channel.")
                return
            except discord.HTTPException:
                pass

        def reaction_check(reaction, user):
            return (
                user == ctx.author
                and reaction.message.id == msg.id
                and str(reaction.emoji) in available_emojis
            )

        try:
            reaction, user = await self.bot.wait_for(
                "reaction_add", timeout=30.0, check=reaction_check
            )
        except asyncio.TimeoutError:
            embed.set_footer(text="Selection timed out.")
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass
            return

        selection = available_emojis.index(str(reaction.emoji))
        selected_entry = entries[selection]

        selected_url = selected_entry.get("webpage_url") or selected_entry.get("url")
        if selected_url and not selected_url.startswith("http"):
            selected_url = f"https://www.youtube.com/watch?v={selected_url}"
        try:
            fetched = await self._fetch_track_info(selected_url)
        except Exception as e:
            await ctx.send(f"❌ Error fetching track details: {e}")
            self.logger.error(
                f"Error fetching selected search track {selected_url}: {e}"
            )
            return

        track_info = {
            "url": fetched.get("url"),
            "title": fetched.get("title", selected_entry.get("title", "Unknown Title")),
            "webpage_url": fetched.get("webpage_url") or selected_url,
            "duration": fetched.get("duration", selected_entry.get("duration")),
        }
        if not track_info["url"]:
            await ctx.send("❌ Couldn't find a playable source for that selection.")
            return

        queue = self.get_guild_queue(gid)
        embed.description = "\n".join(
            [
                f"**{lines[selection]}** ← selected" if i == selection else line
                for i, line in enumerate(lines)
            ]
        )
        embed.set_footer(
            text=(
                f"Selected {selection + 1}: {track_info.get('title', 'Unknown Title')} "
                f"({self._format_duration(track_info.get('duration'))})"
            )
        )
        try:
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

        try:
            await msg.remove_reaction(reaction.emoji, user)
        except (discord.Forbidden, discord.HTTPException):
            pass

        if ctx.voice_client.is_playing():
            queue.append(track_info)
            await ctx.send(f"➕ Added to queue: **{track_info['title']}**")
        else:
            try:
                await self._start_track(ctx, gid, track_info, announce=False)
                await ctx.send(f"🎶 Now playing: **{track_info['title']}**")
            except Exception as e:
                await ctx.send(f"❌ Error starting playback for selected track: {e}")
                self.logger.error(
                    f"Error playing selected search track {track_info.get('title', 'N/A')}: {e}"
                )




    @commands.command(
        help="Display the current queue.\nUsage: !queue\nShows up to 10 upcoming tracks."
    )
    async def queue_list(self, ctx):
        gid = str(ctx.guild.id)
        queue = self.queues.get(gid, [])
        loading_queue = self.loading_queues.get(gid, [])

        msg = ""
        current = self.current_tracks.get(gid)
        if current:
            duration = current.get("duration")
            duration_str = f" ({self._format_duration(duration)})" if duration else ""
            msg += f"🎵 **Now Playing:** {current['title']}{duration_str}\n\n"

        if not queue and not loading_queue and not current:
            await ctx.send("📭 The queue is empty and nothing is loading.")
            return

        if queue:
            msg += f"📋 **Queue ({len(queue)} song(s)):**\n"
            for i, track in enumerate(queue[:10], start=1):
                duration = track.get("duration")
                duration_str = (
                    f" ({self._format_duration(duration)})" if duration else ""
                )
                msg += f"{i}. {track['title']}{duration_str}\n"
            if len(queue) > 10:
                msg += f"... and {len(queue) - 10} more.\n"
        else:
            msg += "📭 **Queue is empty.**\n"

        if loading_queue:
            task_running = (
                gid in self.loading_tasks and not self.loading_tasks[gid].done()
            )
            status = "currently loading" if task_running else "pending load"
            msg += f"\n⏳ **({len(loading_queue)} track(s) {status}...)**"

        await ctx.send(msg)

    @commands.command(help="Show the currently playing track.\nUsage: !np")
    async def np(self, ctx):
        gid = str(ctx.guild.id)
        current = self.current_tracks.get(gid)
        if current:
            duration = current.get("duration")
            duration_str = f" ({self._format_duration(duration)})" if duration else ""
            position = self._get_current_position(gid)
            position_str = f" - {self._format_duration(position)}" if position else ""
            await ctx.send(
                f"🎵 **Now playing:** {current['title']}{duration_str}{position_str}"
            )
        else:
            await ctx.send("❌ No track is currently playing.")

    @commands.command(help="Skip the current track.\nUsage: !skip")
    async def skip(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("⏭️ Track skipped.")
        elif ctx.voice_client:
            await ctx.send("❌ Nothing is currently playing to skip.")
        else:
            await ctx.send("❌ I'm not connected to a voice channel.")

    @commands.command(
        help="Remove tracks from the queue.\nUsage: !remove <position>\nOptions:\n- Number (1-10): Remove track at position\n- 'first': Remove first track\n- 'last': Remove last track\n- 'all': Clear queue & loading queue\n- Text: Remove first matching track\nExample: !remove 3"
    )
    async def remove(self, ctx, arg: str):
        gid = str(ctx.guild.id)
        queue = self.get_guild_queue(gid)
        loading_queue = self.loading_queues.get(gid, [])
        loading_lock = self.loading_locks.get(gid)

        if not queue and not loading_queue:
            await ctx.send("📭 The queue is empty.")
            return

        arg_lower = arg.lower()
        removed_title = "Unknown Title"

        if arg_lower == "all":
            count = len(queue)
            loading_count = 0
            self.queues[gid] = []
            if loading_lock:
                async with loading_lock:
                    loading_count = len(loading_queue)
                    self.loading_queues[gid] = []
            if gid in self.loading_tasks:
                try:
                    if not self.loading_tasks[gid].done():
                        self.loading_tasks[gid].cancel()
                        self.logger.info(
                            f"Cancelled background loader task for GID {gid} due to !remove all."
                        )
                    del self.loading_tasks[gid]
                except KeyError:
                    pass
                except Exception as e:
                    self.logger.error(
                        f"Error cancelling background task for GID {gid}: {e}"
                    )

            await ctx.send(
                f"🗑️ Cleared the queue. Removed {count} track(s) and {loading_count} loading track(s)."
            )
            return

        elif arg_lower == "first":
            if queue:
                removed = queue.pop(0)
                removed_title = removed.get("title", removed_title)
            else:
                await ctx.send("📭 Queue is empty, cannot remove first.")
                return
        elif arg_lower == "last":
            if queue:
                removed = queue.pop(-1)
                removed_title = removed.get("title", removed_title)
            else:
                await ctx.send("📭 Queue is empty, cannot remove last.")
                return
        else:
            try:
                index = int(arg)
                if index < 1 or index > len(queue):
                    await ctx.send(f"❌ Index must be between 1 and {len(queue)}.")
                    return
                removed = queue.pop(index - 1)
                removed_title = removed.get("title", removed_title)
            except ValueError:
                search_term = arg_lower
                found_index = -1
                for i, track in enumerate(queue):
                    if search_term in track.get("title", "").lower():
                        found_index = i
                        break

                if found_index != -1:
                    removed = queue.pop(found_index)
                    removed_title = removed.get("title", removed_title)
                else:
                    await ctx.send(
                        "❌ No track found in the queue matching that index or title."
                    )
                    return
            except IndexError:
                await ctx.send("❌ Invalid index specified.")
                return

        await ctx.send(f"🗑️ Removed track: **{removed_title}**")

    @commands.command(
        help="Set the music volume.\nUsage: !volume <0-150>\nExample: !volume 50\nVolume setting persists between sessions."
    )
    async def volume(self, ctx, vol: int = None):
        gid = str(ctx.guild.id)
        if vol is None:
            current_volume = int(self.volumes.get(gid, 1.0) * 100)
            await ctx.send(
                f"🔊 Current volume for this server is **{current_volume}%**."
            )
            return
        if vol < 0 or vol > 150:
            await ctx.send("❌ Volume must be between 0 and 150.")
            return

        target_volume = vol / 100.0
        self.volumes[gid] = target_volume

        try:
            with open(self.volumes_file, "w") as f:
                json.dump(self.volumes, f, indent=4)
        except Exception as e:
            self.logger.error(f"Failed to save volumes to {self.volumes_file}: {e}")

        if (
            ctx.voice_client
            and ctx.voice_client.source
            and isinstance(ctx.voice_client.source, discord.PCMVolumeTransformer)
        ):
            ctx.voice_client.source.volume = target_volume
        await ctx.send(f"🔊 Volume set to **{vol}%** for this server.")

    @commands.command(help="Stop playback and disconnect the bot.\nUsage: !stop")
    async def stop(self, ctx):
        gid = str(ctx.guild.id)
        if gid in self.loading_tasks:
            try:
                if not self.loading_tasks[gid].done():
                    self.loading_tasks[gid].cancel()
                    self.logger.info(
                        f"Cancelled background loader task for GID {gid} due to !stop."
                    )
                del self.loading_tasks[gid]
            except KeyError:
                pass
            except Exception as e:
                self.logger.error(
                    f"Error cancelling background task during stop for GID {gid}: {e}"
                )

        if gid in self.queues:
            self.queues[gid] = []
        if gid in self.loading_locks:
            try:
                if gid in self.loading_queues:
                    self.loading_queues[gid] = []
            except Exception as e:
                self.logger.error(
                    f"Error clearing loading queue during stop for GID {gid}: {e}"
                )

        if gid in self.current_tracks:
            self.current_tracks[gid] = None

        if ctx.voice_client:
            try:
                await ctx.voice_client.disconnect()
                await ctx.send("⏹️ Stopped playback and disconnected.")
            except Exception as e:
                await ctx.send(f"❌ Error disconnecting: {e}")
                self.logger.error(f"Error disconnecting GID {gid}: {e}")
        else:
            await ctx.send(
                "⏹️ I'm not connected to a voice channel, but queues have been cleared."
            )

    @commands.command(help="Pause the current track.\nUsage: !pause")
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            gid = str(ctx.guild.id)
            loop = self.bot.loop
            self.pause_start_time[gid] = loop.time()
            await ctx.send("⏸️ Music has been paused.")
        elif ctx.voice_client and ctx.voice_client.is_paused():
            await ctx.send("⏸️ Music is already paused.")
        else:
            await ctx.send("❌ No music is currently playing.")

    @commands.command(help="Resume the paused track.\nUsage: !resume")
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            gid = str(ctx.guild.id)
            loop = self.bot.loop
            pause_start = self.pause_start_time.pop(gid, None)
            if pause_start and gid in self.playback_start_time:
                pause_duration = loop.time() - pause_start
                self.playback_start_time[gid] += pause_duration
                self.logger.debug(
                    f"Resumed GID {gid}. Adjusted start time by {pause_duration:.2f}s."
                )
            elif pause_start:
                self.logger.warning(
                    f"Resumed GID {gid} but playback_start_time was missing."
                )
            await ctx.send("▶️ Music has been resumed.")
        elif ctx.voice_client and ctx.voice_client.is_playing():
            await ctx.send("▶️ Music is already playing.")
        else:
            await ctx.send("❌ Music is not paused or playing.")


async def setup(bot):
    await bot.add_cog(MusicCommands(bot))
