import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import os
import json
from cogs.admin import is_admin
import functools  # Added for run_in_executor partials
from logger import get_logger  # Added for logging
import asyncio  # Added for background tasks, locks, events

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
        # Correctly locate volumes.json within the src directory
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
        self.loading_queues = {}  # Stores URLs/IDs being processed in the background
        self.loading_tasks = {}  # Stores background asyncio tasks for loading
        self.loading_locks = {}  # Locks for accessing loading_queues
        self.play_next_events = (
            {}
        )  # Events to signal play_next when loader adds a track

    def get_guild_queue(self, gid):
        # Initialize guild-specific structures if they don't exist
        if gid not in self.queues:
            self.queues[gid] = []
        if gid not in self.loading_queues:
            self.loading_queues[gid] = []
        if gid not in self.loading_locks:
            self.loading_locks[gid] = asyncio.Lock()
        if gid not in self.play_next_events:
            self.play_next_events[gid] = asyncio.Event()
        return self.queues[gid]

    async def _fetch_track_info(self, url_or_id):
        """Fetches full track info for a single URL or ID. Runs in executor."""
        loop = self.bot.loop
        # Use default YDL_OPTIONS (no flat extract) for single track details
        ydl_opts_single = YDL_OPTIONS.copy()
        ydl_opts_single["noplaylist"] = True  # Ensure we only get one item
        ydl = youtube_dl.YoutubeDL(ydl_opts_single)
        try:
            data = await loop.run_in_executor(
                None, functools.partial(ydl.extract_info, url_or_id, download=False)
            )
            if (
                "entries" in data
            ):  # Should not happen with noplaylist=True, but safety first
                data = data["entries"][0]
            # Try different ways to get the audio URL
            audio_url = data.get("url")
            if not audio_url:
                formats = data.get("formats", [])
                for f in formats:
                    # Prioritize audio-only formats if available
                    if f.get("acodec") != "none" and f.get("vcodec") == "none":
                        audio_url = f.get("url")
                        break
                if (
                    not audio_url and formats
                ):  # Fallback to first format URL if no audio-only found
                    audio_url = formats[0].get("url")

            if not audio_url:
                self.logger.error(
                    f"Could not find playable audio URL for {url_or_id} in data: {data.get('title', 'N/A')}"
                )
                return None
            track = {"url": audio_url, "title": data.get("title", "Unknown Title")}
            return track
        except Exception as e:
            self.logger.error(f"Error fetching single track info for {url_or_id}: {e}")
            return None

    async def play_next(self, ctx, gid):
        """Plays the next track in the queue or waits for the background loader."""
        queue = self.get_guild_queue(gid)
        play_next_event = self.play_next_events.get(gid)  # Get event for this guild

        if queue:
            volume = self.volumes.get(gid, 1.0)
            next_track = queue.pop(0)

            # Ensure voice client still exists and is connected
            vc = ctx.guild.voice_client
            if not vc or not vc.is_connected():
                self.logger.warning(
                    f"play_next called for GID {gid} but voice client is disconnected."
                )
                self.current_tracks[gid] = None  # Ensure state is clean
                # Clear queues as well? Maybe not, user might reconnect.
                return

            try:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(next_track["url"], **FFMPEG_OPTIONS),
                    volume=volume,
                )
                vc.play(
                    source,
                    after=lambda e: self.handle_after_play(
                        e, ctx, gid
                    ),  # Use a helper for error handling
                )
                self.current_tracks[gid] = next_track
                # Avoid sending message if context is unavailable (e.g., bot restarted)
                if ctx and ctx.channel:
                    await ctx.send(f"Now playing: **{next_track['title']}**")
            except Exception as e:
                if ctx and ctx.channel:
                    await ctx.send(f"Error playing next track: {e}")
                self.logger.error(
                    f"Error in play_next starting track {next_track.get('title', 'N/A')}: {e}"
                )
                # Try playing the next one if possible by scheduling play_next again
                self.bot.loop.create_task(self.play_next(ctx, gid))

        else:
            # Queue is empty, check if background loader is running
            loader_task = self.loading_tasks.get(gid)
            if loader_task and not loader_task.done():
                self.logger.info(
                    f"play_next for GID {gid}: Queue empty, waiting for background loader..."
                )
                try:
                    # Wait for the event set by the loader, with a timeout
                    await asyncio.wait_for(play_next_event.wait(), timeout=10.0)
                    # Event was set, loader likely added a track. Clear event and retry play_next.
                    play_next_event.clear()
                    self.logger.info(
                        f"play_next for GID {gid}: Loader added track, retrying..."
                    )
                    self.bot.loop.create_task(
                        self.play_next(ctx, gid)
                    )  # Schedule retry
                except asyncio.TimeoutError:
                    # Loader didn't add a track within the timeout. Assume queue is finished.
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
                    self.current_tracks[gid] = None  # Ensure clean state on error
            else:
                # Queue is empty and loader is not running (or doesn't exist)
                self.logger.info(
                    f"play_next for GID {gid}: Queue empty and no active loader. Playback finished."
                )
                self.current_tracks[gid] = None
                # Avoid sending "Queue finished" if nothing was ever played or just stopped
                # We might need more state tracking if this message is desired only after playing tracks.

    def handle_after_play(self, error, ctx, gid):
        """Callback function for after a track finishes playing or errors."""
        if error:
            self.logger.error(f"Error after playing track for GID {gid}: {error}")
            # Optionally send a message to the channel about the error
            # Note: ctx might be invalid if the bot restarted, handle potential errors
            # coro = ctx.send(f"An error occurred during playback: {error}")
            # fut = asyncio.run_coroutine_threadsafe(coro, self.bot.loop)
            # try:
            #     fut.result(timeout=5) # Add timeout to prevent blocking
            # except Exception as e:
            #      self.logger.error(f"Error sending 'after_play' error message for GID {gid}: {e}")

        # Always schedule play_next to check for the next song or wait for loader
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
        help="Play music from YouTube.\nUsage: !play <URL>\nSupports single videos and playlists.\nExample: !play https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    async def play(self, ctx, url: str):
        if not ctx.voice_client:
            if ctx.author.voice:
                if not await self._ensure_voice(ctx):
                    return  # Failed to join/move
            else:
                await ctx.send("You must be in a voice channel to play audio.")
                return
        elif not await self._ensure_voice(
            ctx
        ):  # Ensure bot is in the correct channel if already connected
            return
        # Run blocking youtube_dl call in executor
        await ctx.send(f"Processing request for <{url}>...")  # Initial feedback
        loop = self.bot.loop
        try:
            # Use 'extract_flat' to quickly get playlist entries without full processing
            ydl_opts_flat = YDL_OPTIONS.copy()
            ydl_opts_flat["extract_flat"] = (
                "in_playlist"  # Get URLs for playlist entries
            )
            ydl = youtube_dl.YoutubeDL(ydl_opts_flat)
            # Need to use a lambda or partial as run_in_executor doesn't directly accept args for the func
            info = await loop.run_in_executor(
                None,
                functools.partial(
                    ydl.extract_info, url, download=False
                ),  # Use flat opts here
            )
        except youtube_dl.utils.DownloadError as e:  # Catch specific ytdl errors
            await ctx.send(f"Error processing the URL: {e}")
            self.logger.error(f"Error processing URL {url}: {e}")
            return
        except Exception as e:  # Catch other potential errors during extraction
            await ctx.send(f"An unexpected error occurred while processing the URL.")
            self.logger.error(f"Unexpected error processing URL {url}: {e}")
            return

        gid = str(ctx.guild.id)
        queue = self.get_guild_queue(gid)  # Ensures guild structures are initialized
        volume = self.volumes.get(gid, 1.0)
        loading_queue = self.loading_queues[gid]
        loading_lock = self.loading_locks[gid]
        play_next_event = self.play_next_events[gid]

        if (
            "entries" in info and info.get("_type") == "playlist"
        ):  # Check it's actually a playlist
            entries = info["entries"]
            if not entries:
                await ctx.send("No videos found in the playlist.")
                return

            # Playlist logic
            # Filter out potential None entries just in case
            valid_entries = [entry for entry in entries if entry and entry.get("url")]
            if not valid_entries:
                await ctx.send("Playlist contains no valid video URLs.")
                return

            first_entry_url = valid_entries[0]["url"]
            remaining_entry_urls = [entry["url"] for entry in valid_entries[1:]]

            if not ctx.voice_client.is_playing():
                # Not playing: Fetch full info for the first track to play immediately
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
                            after=lambda e: self.handle_after_play(
                                e, ctx, gid
                            ),  # Use helper
                        )
                        self.current_tracks[gid] = first_track_info
                        await ctx.send(f"Now playing: **{first_track_info['title']}**")
                    except Exception as e:
                        await ctx.send(
                            f"Error starting playback for the first track: {e}"
                        )
                        self.logger.error(
                            f"Error playing first track {first_track_info.get('title', 'N/A')}: {e}"
                        )
                        # Add first track to loading queue if playback failed
                        remaining_entry_urls.insert(0, first_entry_url)
                else:  # Failed to fetch first track
                    await ctx.send(
                        "Error fetching details for the first track of the playlist."
                    )
                    # Add the first URL to the loading queue as well if fetching failed
                    remaining_entry_urls.insert(0, first_entry_url)

                # Add remaining tracks to the loading queue
                async with loading_lock:
                    loading_queue.extend(remaining_entry_urls)

                if (
                    remaining_entry_urls
                ):  # Only show loading message if there are remaining tracks
                    await ctx.send(
                        f"Loading {len(remaining_entry_urls)} more track(s) in the background..."
                    )
                # Start background loader
                if gid not in self.loading_tasks or self.loading_tasks[gid].done():
                    self.loading_tasks[gid] = asyncio.create_task(
                        self._background_loader(ctx, gid)
                    )

            else:
                # Already playing: Add all URLs to the loading queue
                all_entry_urls = [entry["url"] for entry in valid_entries]
                async with loading_lock:
                    loading_queue.extend(all_entry_urls)
                await ctx.send(
                    f"Added {len(all_entry_urls)} track(s) to the loading queue..."
                )
                # Start background loader
                if gid not in self.loading_tasks or self.loading_tasks[gid].done():
                    self.loading_tasks[gid] = asyncio.create_task(
                        self._background_loader(ctx, gid)
                    )

        else:
            # Single track logic (or non-playlist entry)
            # Need to fetch full info as flat extract doesn't provide it for single items
            single_track_info = await self._fetch_track_info(url)  # Use original URL
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
                        after=lambda e: self.handle_after_play(
                            e, ctx, gid
                        ),  # Use helper
                    )
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
        queue = self.queues.get(gid)  # Main playback queue

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
                # Check if task was cancelled (e.g., by !stop)
                # A simple check if the queue was cleared externally might suffice
                if gid not in self.queues:  # Check if main queue dict was deleted
                    self.logger.info(
                        f"Background loader for GID {gid} stopping as queues were cleared."
                    )
                    break

                async with loading_lock:
                    if loading_queue:
                        url_to_process = loading_queue.pop(0)
                    else:
                        # Loading queue is empty, task can finish
                        break
            except Exception as e:
                self.logger.error(
                    f"Error accessing loading queue/lock for GID {gid}: {e}"
                )
                break  # Stop task on error

            if url_to_process:
                track_info = await self._fetch_track_info(url_to_process)
                if track_info:
                    # Check again if queues were cleared between fetch and append
                    if gid in self.queues:
                        queue.append(track_info)
                        processed_count += 1
                        play_next_event.set()  # Signal play_next that a new track is available
                        play_next_event.clear()  # Reset event immediately after setting
                    else:
                        self.logger.info(
                            f"Background loader for GID {gid} stopping mid-process as queues were cleared."
                        )
                        break
                else:
                    error_count += 1
                    # Optionally notify the user about the failed track?
                    # await ctx.send(f"Failed to load details for one track.")
                    self.logger.warning(
                        f"Failed to fetch info for {url_to_process} in background loader for GID {gid}."
                    )

            # Yield control to the event loop briefly
            await asyncio.sleep(0.1)

        self.logger.info(
            f"Background loader finished for GID {gid}. Processed: {processed_count}, Errors: {error_count}."
        )
        # Optional: Notify user that loading is complete?
        # try:
        #     if processed_count > 0 or error_count > 0: # Only notify if something happened
        #        if ctx and ctx.channel: # Check if context is still valid
        #             await ctx.send(f"Finished loading playlist tracks in the background ({processed_count} added, {error_count} failed).")
        # except discord.NotFound: # Context might be gone
        #     pass
        # except Exception as e:
        #     self.logger.error(f"Error sending background loader completion message for GID {gid}: {e}")

        # Clean up task reference
        if gid in self.loading_tasks:
            try:
                del self.loading_tasks[gid]
            except KeyError:
                pass  # Already deleted, maybe by stop command

    @commands.command(
        help="Search YouTube and choose a song to play.\nUsage: !search <query>\nShows top 5 results and lets you choose.\nExample: !search never gonna give you up"
    )
    async def search(self, ctx, *, query: str):
        # Keep search simple for now - doesn't use background loading
        if not ctx.voice_client:
            if ctx.author.voice:
                if not await self._ensure_voice(ctx):
                    return  # Failed to join/move
            else:
                await ctx.send("You must be in a voice channel to play audio.")
                return
        elif not await self._ensure_voice(
            ctx
        ):  # Ensure bot is in the correct channel if already connected
            return
        gid = str(ctx.guild.id)
        loop = self.bot.loop
        await ctx.send(f"Searching YouTube for '{query}'...")
        try:
            # Search doesn't need flat extract, get top 5 directly
            ydl_opts_search = YDL_OPTIONS.copy()
            ydl_opts_search["noplaylist"] = (
                True  # Ensure search doesn't expand playlists
            )
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

        # Fetch full info for the selected track
        track_info = await self._fetch_track_info(selected_entry["webpage_url"])
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
                    after=lambda e: self.handle_after_play(e, ctx, gid),  # Use helper
                )
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
            # Check if the loading task is actually running
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
            ctx.voice_client.stop()  # This will trigger the 'after' callback -> handle_after_play() -> play_next()
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
        loading_lock = self.loading_locks.get(
            gid
        )  # Might not exist if queue never initialized

        if not queue and not loading_queue:  # Check both queues
            await ctx.send("The queue is empty.")
            return

        arg_lower = arg.lower()
        removed_title = "Unknown Title"

        if arg_lower == "all":
            count = len(queue)
            loading_count = 0
            self.queues[gid] = []
            if loading_lock:  # Need lock to clear loading queue safely
                async with loading_lock:
                    loading_count = len(loading_queue)
                    self.loading_queues[gid] = []
            # Cancel background loader task if running
            if gid in self.loading_tasks:
                try:
                    if not self.loading_tasks[gid].done():
                        self.loading_tasks[gid].cancel()
                        self.logger.info(
                            f"Cancelled background loader task for GID {gid} due to !remove all."
                        )
                    del self.loading_tasks[gid]  # Remove reference
                except KeyError:
                    pass  # Task already gone
                except Exception as e:
                    self.logger.error(
                        f"Error cancelling background task for GID {gid}: {e}"
                    )

            await ctx.send(
                f"Cleared the queue. Removed {count} track(s) and {loading_count} loading track(s)."
            )
            return  # Exit after clearing all

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
            try:  # Try removing by index first
                index = int(arg)
                if index < 1 or index > len(queue):  # Check against actual queue length
                    await ctx.send(f"Index must be between 1 and {len(queue)}.")
                    return
                removed = queue.pop(index - 1)
                removed_title = removed.get("title", removed_title)
            except ValueError:  # Not a number, try removing by title match
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
                    # Optionally check loading queue? For now, keep it simple.
                    await ctx.send(
                        "No track found in the queue matching that index or title."
                    )
                    return  # Exit if nothing found
            except IndexError:  # Should be caught by length check, but just in case
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

        # Save volume setting
        try:
            with open(self.volumes_file, "w") as f:
                json.dump(self.volumes, f, indent=4)  # Add indent for readability
        except Exception as e:
            self.logger.error(f"Failed to save volumes to {self.volumes_file}: {e}")
            # Optionally inform user? For now, just log it.

        # Apply volume to current playback if possible
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
        # Cancel background loader task if running
        if gid in self.loading_tasks:
            try:
                if not self.loading_tasks[gid].done():
                    self.loading_tasks[gid].cancel()
                    self.logger.info(
                        f"Cancelled background loader task for GID {gid} due to !stop."
                    )
                del self.loading_tasks[gid]  # Remove reference
            except KeyError:
                pass  # Task already gone
            except Exception as e:
                self.logger.error(
                    f"Error cancelling background task during stop for GID {gid}: {e}"
                )

        # Clear queues immediately
        if gid in self.queues:
            self.queues[gid] = []
        if gid in self.loading_locks:  # Need lock to clear loading queue safely
            try:
                # No need to acquire lock if we are clearing anyway
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


async def setup(bot):
    await bot.add_cog(MusicCommands(bot))
