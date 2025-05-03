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

youtube_dl.utils.bug_reports_message = lambda: ""
YDL_OPTIONS = {"format": "bestaudio/best", "noplaylist": False, "quiet": True}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
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

    async def _fetch_track_info(self, url_or_id):
        """Fetches full track info for a single URL or ID. Runs in executor."""
        loop = self.bot.loop
        ydl_opts_single = YDL_OPTIONS.copy()
        ydl_opts_single["noplaylist"] = True
        ydl = youtube_dl.YoutubeDL(ydl_opts_single)
        try:
            data = await loop.run_in_executor(
                None, functools.partial(ydl.extract_info, url_or_id, download=False)
            )
            if "entries" in data:
                data = data["entries"][0]
            audio_url = data.get("url")
            if not audio_url:
                formats = data.get("formats", [])
                for f in formats:
                    if f.get("acodec") != "none" and f.get("vcodec") == "none":
                        audio_url = f.get("url")
                        break
                if not audio_url and formats:
                    audio_url = formats[0].get("url")

            if not audio_url:
                self.logger.error(
                    f"Could not find playable audio URL for {url_or_id} in data: {data.get('title', 'N/A')}"
                )
                return None
            track = {
                "url": audio_url,
                "title": data.get("title", "Unknown Title"),
                "duration": data.get("duration"),
                "webpage_url": data.get("webpage_url"),
            }
            return track
        except Exception as e:
            self.logger.error(f"Error fetching single track info for {url_or_id}: {e}")
            return None

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

            try:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(next_track["url"], **FFMPEG_OPTIONS),
                    volume=volume,
                )
                vc.play(
                    source,
                    after=lambda e: self.handle_after_play(e, ctx, gid),
                )
                loop = self.bot.loop
                self.playback_start_time[gid] = loop.time()
                self.playback_seek_position[gid] = 0.0
                self.pause_start_time.pop(gid, None)
                self.current_tracks[gid] = next_track
                if ctx and ctx.channel:
                    await ctx.send(f"Now playing: **{next_track['title']}**")
            except Exception as e:
                if ctx and ctx.channel:
                    await ctx.send(f"Error playing next track: {e}")
                self.logger.error(
                    f"Error in play_next starting track {next_track.get('title', 'N/A')}: {e}"
                )
                self.bot.loop.create_task(self.play_next(ctx, gid))

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
        help="Join a voice channel.\nUsage: !join\nMust be in a voice channel to use this command."
    )
    async def join(self, ctx):
        if not ctx.author.voice:
            await ctx.send("You are not connected to a voice channel.")
            return
        await self._ensure_voice(ctx)

    @commands.command(
        help="Play music from YouTube or search.\nUsage: !play <URL or Search Query>\nSupports videos, playlists, and search terms.\nExample: !play never gonna give you up"
    )
    async def play(self, ctx, *, query_or_url: str):
        if not ctx.voice_client:
            if ctx.author.voice:
                if not await self._ensure_voice(ctx):
                    return
            else:
                await ctx.send("You must be in a voice channel to play audio.")
                return
        elif not await self._ensure_voice(ctx):
            return

        url_pattern = re.compile(r"https?://[^\s/$.?#].[^\s]*")
        is_url = url_pattern.match(query_or_url)
        url_to_process = query_or_url

        if not is_url:
            await ctx.send(f"Searching for '{query_or_url}'...")
            loop = self.bot.loop
            try:
                ydl_opts_search = YDL_OPTIONS.copy()
                ydl_opts_search["noplaylist"] = True
                ydl = youtube_dl.YoutubeDL(ydl_opts_search)
                search_info = await loop.run_in_executor(
                    None,
                    functools.partial(
                        ydl.extract_info, f"ytsearch1:{query_or_url}", download=False
                    ),
                )
                entries = search_info.get("entries", [])
                if not entries:
                    await ctx.send(f"No results found for '{query_or_url}'.")
                    return
                first_result = entries[0]
                url_to_process = first_result.get("webpage_url") or first_result.get(
                    "url"
                )
                if not url_to_process:
                    await ctx.send(
                        f"Could not find a playable URL for the top result of '{query_or_url}'."
                    )
                    return
                await ctx.send(
                    f"Found: **{first_result.get('title', 'Unknown Title')}**. Processing..."
                )
            except Exception as e:
                await ctx.send(f"Error during search: {e}")
                self.logger.error(
                    f"Error processing implicit search '{query_or_url}': {e}"
                )
                return
        else:
            await ctx.send(f"Processing URL <{url_to_process}>...")

        loop = self.bot.loop
        try:
            ydl_opts_flat = YDL_OPTIONS.copy()
            ydl_opts_flat["extract_flat"] = "in_playlist"
            ydl = youtube_dl.YoutubeDL(ydl_opts_flat)
            info = await loop.run_in_executor(
                None,
                functools.partial(ydl.extract_info, url_to_process, download=False),
            )
        except youtube_dl.utils.DownloadError as e:
            await ctx.send(f"Error processing the URL: {e}")
            self.logger.error(f"Error processing URL/Query '{query_or_url}': {e}")
            return
        except Exception as e:
            await ctx.send(f"An unexpected error occurred while processing the URL.")
            self.logger.error(
                f"Unexpected error processing URL/Query '{query_or_url}': {e}"
            )
            return

        gid = str(ctx.guild.id)
        queue = self.get_guild_queue(gid)
        volume = self.volumes.get(gid, 1.0)
        loading_queue = self.loading_queues[gid]
        loading_lock = self.loading_locks[gid]
        play_next_event = self.play_next_events[gid]

        if "entries" in info and info.get("_type") == "playlist":
            entries = info["entries"]
            if not entries:
                await ctx.send("No videos found in the playlist.")
                return

            valid_entries = [entry for entry in entries if entry and entry.get("url")]
            if not valid_entries:
                await ctx.send("Playlist contains no valid video URLs.")
                return

            first_entry_url = valid_entries[0]["url"]
            remaining_entry_urls = [entry["url"] for entry in valid_entries[1:]]

            if not ctx.voice_client.is_playing():
                first_track_info = await self._fetch_track_info(first_entry_url)
                if first_track_info:
                    try:
                        source = discord.PCMVolumeTransformer(
                            discord.FFmpegPCMAudio(
                                first_track_info["url"], **FFMPEG_OPTIONS
                            ),
                            volume=volume,
                        )
                        ctx.voice_client.play(
                            source,
                            after=lambda e: self.handle_after_play(e, ctx, gid),
                        )
                        loop = self.bot.loop
                        self.playback_start_time[gid] = loop.time()
                        self.playback_seek_position[gid] = 0.0
                        self.pause_start_time.pop(gid, None)
                        self.current_tracks[gid] = first_track_info
                        await ctx.send(f"Now playing: **{first_track_info['title']}**")
                    except Exception as e:
                        await ctx.send(
                            f"Error starting playback for the first track: {e}"
                        )
                        self.logger.error(
                            f"Error playing first track {first_track_info.get('title', 'N/A')}: {e}"
                        )
                        remaining_entry_urls.insert(0, first_entry_url)
                else:
                    await ctx.send(
                        "Error fetching details for the first track of the playlist."
                    )
                    remaining_entry_urls.insert(0, first_entry_url)

                async with loading_lock:
                    loading_queue.extend(remaining_entry_urls)

                if remaining_entry_urls:
                    await ctx.send(
                        f"Loading {len(remaining_entry_urls)} more track(s) in the background..."
                    )
                if gid not in self.loading_tasks or self.loading_tasks[gid].done():
                    self.loading_tasks[gid] = asyncio.create_task(
                        self._background_loader(ctx, gid)
                    )

            else:
                all_entry_urls = [entry["url"] for entry in valid_entries]
                async with loading_lock:
                    loading_queue.extend(all_entry_urls)
                await ctx.send(
                    f"Added {len(all_entry_urls)} track(s) to the loading queue..."
                )
                if gid not in self.loading_tasks or self.loading_tasks[gid].done():
                    self.loading_tasks[gid] = asyncio.create_task(
                        self._background_loader(ctx, gid)
                    )

        else:
            single_track_info = await self._fetch_track_info(url_to_process)
            if not single_track_info:
                await ctx.send(
                    f"Could not fetch details or find playable audio for the requested URL."
                )
                return

            track = single_track_info
            if ctx.voice_client.is_playing():
                queue.append(track)
                await ctx.send(f"Added to queue: **{track['title']}**")
            else:
                try:
                    source = discord.PCMVolumeTransformer(
                        discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTIONS),
                        volume=volume,
                    )
                    ctx.voice_client.play(
                        source,
                        after=lambda e: self.handle_after_play(e, ctx, gid),
                    )
                    loop = self.bot.loop
                    self.playback_start_time[gid] = loop.time()
                    self.playback_seek_position[gid] = 0.0
                    self.pause_start_time.pop(gid, None)
                    self.current_tracks[gid] = track
                    await ctx.send(f"Now playing: **{track['title']}**")
                except Exception as e:
                    await ctx.send(f"Error starting playback: {e}")
                    self.logger.error(
                        f"Error playing single track {track.get('title', 'N/A')}: {e}"
                    )

    async def _background_loader(self, ctx, gid):
        """Processes URLs from the loading queue and adds them to the main queue."""
        loading_queue = self.loading_queues.get(gid, [])
        loading_lock = self.loading_locks.get(gid)
        play_next_event = self.play_next_events.get(gid)
        queue = self.queues.get(gid)

        if not loading_lock or not play_next_event or queue is None:
            self.logger.error(
                f"Background loader for GID {gid} missing required structures."
            )
            return

        self.logger.info(f"Background loader started for GID {gid}.")
        processed_count = 0
        error_count = 0

        while True:
            url_to_process = None
            try:
                if gid not in self.queues:
                    self.logger.info(
                        f"Background loader for GID {gid} stopping as queues were cleared."
                    )
                    break

                async with loading_lock:
                    if loading_queue:
                        url_to_process = loading_queue.pop(0)
                    else:
                        break
            except Exception as e:
                self.logger.error(
                    f"Error accessing loading queue/lock for GID {gid}: {e}"
                )
                break

            if url_to_process:
                track_info = await self._fetch_track_info(url_to_process)
                if track_info:
                    if gid in self.queues:
                        queue.append(track_info)
                        processed_count += 1
                        play_next_event.set()
                        play_next_event.clear()
                    else:
                        self.logger.info(
                            f"Background loader for GID {gid} stopping mid-process as queues were cleared."
                        )
                        break
                else:
                    error_count += 1
                    self.logger.warning(
                        f"Failed to fetch info for {url_to_process} in background loader for GID {gid}."
                    )

            await asyncio.sleep(0.1)

        self.logger.info(
            f"Background loader finished for GID {gid}. Processed: {processed_count}, Errors: {error_count}."
        )

        if gid in self.loading_tasks:
            try:
                del self.loading_tasks[gid]
            except KeyError:
                pass

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
        await ctx.send(f"Searching YouTube for '{query}'...")
        try:
            ydl_opts_search = YDL_OPTIONS.copy()
            ydl_opts_search["noplaylist"] = True
            ydl = youtube_dl.YoutubeDL(ydl_opts_search)

            info = await loop.run_in_executor(
                None,
                functools.partial(
                    ydl.extract_info, f"ytsearch5:{query}", download=False
                ),
            )
        except Exception as e:
            await ctx.send(f"Error processing search: {e}")
            self.logger.error(f"Error processing search '{query}': {e}")
            return

        entries = info.get("entries", [])
        if not entries:
            await ctx.send("No results found.")
            return
        msg = "Search results:\n"
        for i, entry in enumerate(entries, start=1):
            msg += f"{i}. {entry.get('title', 'Unknown Title')}\n"
        msg += "Type the number of the video you want to play (1-5)."
        await ctx.send(msg)

        def check(m):
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.isdigit()
                and int(m.content) in range(1, len(entries) + 1)
            )

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.send("Selection timed out.")
            return
        selection = int(reply.content)
        selected_entry = entries[selection - 1]

        track_info = {
            "url": selected_entry.get("url"),
            "title": selected_entry.get("title", "Unknown Title"),
            "webpage_url": selected_entry.get("webpage_url"),
        }

        if not track_info.get("url"):
            self.logger.info(
                f"Direct URL not found for '{track_info['title']}' in search results, fetching full info..."
            )
            track_info = await self._fetch_track_info(track_info["webpage_url"])
            if not track_info:
                await ctx.send(f"Error fetching details for the selected video.")
                return

        volume = self.volumes.get(gid, 1.0)
        queue = self.get_guild_queue(gid)
        if ctx.voice_client.is_playing():
            queue.append(track_info)
            await ctx.send(f"Added to queue: **{track_info['title']}**")
        else:
            try:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(track_info["url"], **FFMPEG_OPTIONS),
                    volume=volume,
                )
                ctx.voice_client.play(
                    source,
                    after=lambda e: self.handle_after_play(e, ctx, gid),
                )
                loop = self.bot.loop
                self.playback_start_time[gid] = loop.time()
                self.playback_seek_position[gid] = 0.0
                self.pause_start_time.pop(gid, None)
                self.current_tracks[gid] = track_info
                await ctx.send(f"Now playing: **{track_info['title']}**")
            except Exception as e:
                await ctx.send(f"Error starting playback for selected track: {e}")
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
            msg += f"**Now Playing:** {current['title']}\n\n"

        if not queue and not loading_queue and not current:
            await ctx.send("The queue is empty and nothing is loading.")
            return

        if queue:
            msg += f"**Queue ({len(queue)} song(s)):**\n"
            for i, track in enumerate(queue[:10], start=1):
                msg += f"{i}. {track['title']}\n"
            if len(queue) > 10:
                msg += f"... and {len(queue) - 10} more.\n"
        else:
            msg += "**Queue is empty.**\n"

        if loading_queue:
            task_running = (
                gid in self.loading_tasks and not self.loading_tasks[gid].done()
            )
            status = "currently loading" if task_running else "pending load"
            msg += f"\n**({len(loading_queue)} track(s) {status}...)**"

        await ctx.send(msg)

    @commands.command(help="Show the currently playing track.\nUsage: !np")
    async def np(self, ctx):
        gid = str(ctx.guild.id)
        current = self.current_tracks.get(gid)
        if current:
            await ctx.send(f"Now playing: **{current['title']}**")
        else:
            await ctx.send("No track is currently playing.")

    @commands.command(help="Skip the current track.\nUsage: !skip")
    async def skip(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
            await ctx.send("Track skipped.")
        elif ctx.voice_client:
            await ctx.send("Nothing is currently playing to skip.")
        else:
            await ctx.send("I'm not connected to a voice channel.")

    @commands.command(
        help="Remove tracks from the queue.\nUsage: !remove <position>\nOptions:\n- Number (1-10): Remove track at position\n- 'first': Remove first track\n- 'last': Remove last track\n- 'all': Clear queue & loading queue\n- Text: Remove first matching track\nExample: !remove 3"
    )
    async def remove(self, ctx, arg: str):
        gid = str(ctx.guild.id)
        queue = self.get_guild_queue(gid)
        loading_queue = self.loading_queues.get(gid, [])
        loading_lock = self.loading_locks.get(gid)

        if not queue and not loading_queue:
            await ctx.send("The queue is empty.")
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
                f"Cleared the queue. Removed {count} track(s) and {loading_count} loading track(s)."
            )
            return

        elif arg_lower == "first":
            if queue:
                removed = queue.pop(0)
                removed_title = removed.get("title", removed_title)
            else:
                await ctx.send("Queue is empty, cannot remove first.")
                return
        elif arg_lower == "last":
            if queue:
                removed = queue.pop(-1)
                removed_title = removed.get("title", removed_title)
            else:
                await ctx.send("Queue is empty, cannot remove last.")
                return
        else:
            try:
                index = int(arg)
                if index < 1 or index > len(queue):
                    await ctx.send(f"Index must be between 1 and {len(queue)}.")
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
                        "No track found in the queue matching that index or title."
                    )
                    return
            except IndexError:
                await ctx.send("Invalid index specified.")
                return

        await ctx.send(f"Removed track: **{removed_title}**")

    @commands.command(
        help="Set the music volume.\nUsage: !volume <0-150>\nExample: !volume 50\nVolume setting persists between sessions."
    )
    async def volume(self, ctx, vol: int = None):
        gid = str(ctx.guild.id)
        if vol is None:
            current_volume = int(self.volumes.get(gid, 1.0) * 100)
            await ctx.send(f"Current volume for this server is {current_volume}%.")
            return
        if vol < 0 or vol > 150:
            await ctx.send("Volume must be between 0 and 150.")
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
        await ctx.send(f"Volume set to {vol}% for this server.")

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
                await ctx.send("Stopped playback and disconnected.")
            except Exception as e:
                await ctx.send(f"Error disconnecting: {e}")
                self.logger.error(f"Error disconnecting GID {gid}: {e}")
        else:
            await ctx.send(
                "I'm not connected to a voice channel, but queues have been cleared."
            )

    @commands.command(help="Pause the current track.\nUsage: !pause")
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Music has been paused.")
        elif ctx.voice_client and ctx.voice_client.is_paused():
            await ctx.send("Music is already paused.")
        else:
            await ctx.send("No music is currently playing.")

    @commands.command(help="Resume the paused track.\nUsage: !resume")
    async def resume(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Music has been resumed.")
        elif ctx.voice_client and ctx.voice_client.is_playing():
            await ctx.send("Music is already playing.")
        else:
            await ctx.send("Music is not paused or playing.")

    @commands.command(
        help="Seek to a specific time in the current track.\nUsage: !seek <[+|-]seconds | HH:MM:SS | MM:SS>\nExamples:\n!seek 1:25\n!seek +10\n!seek -30"
    )
    async def seek(self, ctx, *, time: str):
        gid = str(ctx.guild.id)
        vc = ctx.voice_client

        if not vc:
            await ctx.send("I am not connected to a voice channel.")
            return

        if not vc.is_playing():
            await ctx.send("Nothing is currently playing.")
            return

        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            await ctx.send("You must be in the same voice channel as the bot to seek.")
            return

        current_track = self.current_tracks.get(gid)
        if not current_track:
            await ctx.send("No track information found for the current playback.")
            self.logger.warning(
                f"Seek command: vc is playing but no current_track found for GID {gid}"
            )
            return

        track_duration_seconds = current_track.get("duration")
        if track_duration_seconds is None:
            await ctx.send(
                "Cannot determine the duration of the current track. Seeking not possible."
            )
            return
        try:
            track_duration_seconds = float(track_duration_seconds)
        except (ValueError, TypeError):
            await ctx.send("Track duration is invalid. Seeking not possible.")
            return

        target_seconds = None
        time_str = time.strip()
        is_relative = False

        if time_str.startswith(("+", "-")):
            is_relative = True
            try:
                relative_seconds = int(time_str)
                current_position = self._get_current_position(gid)
                if current_position is not None:
                    target_seconds = current_position + relative_seconds
                    self.logger.debug(
                        f"Relative seek: Current={current_position:.2f}, Relative={relative_seconds}, Target={target_seconds:.2f}"
                    )
                else:
                    await ctx.send(
                        "Could not determine current position for relative seek."
                    )
                    return
            except ValueError:
                pass
            except Exception as e:
                await ctx.send(f"Error getting current position for relative seek: {e}")
                self.logger.error(f"Error in _get_current_position for GID {gid}: {e}")
                return

        if target_seconds is None and not is_relative:
            time_parts = time_str.split(":")
            if 1 < len(time_parts) <= 3:
                try:
                    if len(time_parts) == 3:
                        h, m, s = map(int, time_parts)
                        if h < 0 or m < 0 or m > 59 or s < 0 or s > 59:
                            raise ValueError("Invalid time component value")
                        target_seconds = h * 3600 + m * 60 + s
                    elif len(time_parts) == 2:
                        m, s = map(int, time_parts)
                        if m < 0 or s < 0 or s > 59:
                            raise ValueError("Invalid time component value")
                        target_seconds = m * 60 + s
                except ValueError:
                    pass

        if target_seconds is None and not is_relative:
            try:
                abs_seconds = int(time_str)
                if abs_seconds >= 0:
                    target_seconds = float(abs_seconds)
                else:
                    pass
            except ValueError:
                pass

        if target_seconds is None:
            await ctx.send(
                "Invalid time format. Use `HH:MM:SS`, `MM:SS`, `seconds`, or relative `+/-seconds` (e.g., `1:25`, `90`, `+10`, `-5`)."
            )
            return

        final_seek_time = max(0.0, min(float(target_seconds), track_duration_seconds))
        final_seek_time_int = int(final_seek_time)

        new_ffmpeg_options = FFMPEG_OPTIONS.copy()
        new_ffmpeg_options["before_options"] = (
            f"-ss {final_seek_time_int} {new_ffmpeg_options.get('before_options', '')}".strip()
        )

        volume = self.volumes.get(gid, 1.0)
        original_url = current_track.get("url")

        if not original_url:
            await ctx.send(
                "Error: Could not find the original URL for the current track to seek."
            )
            self.logger.error(
                f"Seek error: Missing 'url' in current_track for GID {gid}"
            )
            return

        try:
            self.is_seeking[gid] = True
            vc.stop()

            new_source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(original_url, **new_ffmpeg_options),
                volume=volume,
            )

            vc.play(
                new_source,
                after=lambda e: self.handle_after_play(e, ctx, gid),
            )

            self.current_tracks[gid] = current_track
            loop = self.bot.loop
            self.playback_start_time[gid] = loop.time()
            self.playback_seek_position[gid] = final_seek_time
            self.pause_start_time.pop(gid, None)
            await ctx.send(f"Seeked to **{self._format_duration(final_seek_time)}**.")

        except Exception as e:
            self.is_seeking.pop(gid, None)
            await ctx.send(f"An error occurred while trying to seek: {e}")
            self.logger.error(
                f"Error during seek operation for GID {gid} to {final_seek_time}: {e}"
            )

    @commands.command(help="Pause the current track.\nUsage: !pause")
    async def pause(self, ctx):
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            gid = str(ctx.guild.id)
            loop = self.bot.loop
            self.pause_start_time[gid] = loop.time()
            await ctx.send("Music has been paused.")
        elif ctx.voice_client and ctx.voice_client.is_paused():
            await ctx.send("Music is already paused.")
        else:
            await ctx.send("No music is currently playing.")

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
            await ctx.send("Music has been resumed.")
        elif ctx.voice_client and ctx.voice_client.is_playing():
            await ctx.send("Music is already playing.")
        else:
            await ctx.send("Music is not paused or playing.")


async def setup(bot):
    await bot.add_cog(MusicCommands(bot))
