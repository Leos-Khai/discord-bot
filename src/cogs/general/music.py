import discord
from discord.ext import commands
import yt_dlp as youtube_dl
import os
import json
from cogs.general.admin import is_admin

youtube_dl.utils.bug_reports_message = lambda: ""
YDL_OPTIONS = {"format": "bestaudio/best", "noplaylist": False, "quiet": True}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class MusicCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.volumes_file = os.path.join(script_dir, "..", "..", "volumes.json")
        if os.path.exists(self.volumes_file):
            try:
                with open(self.volumes_file, "r") as f:
                    self.volumes = json.load(f)
            except:
                self.volumes = {}
        else:
            self.volumes = {}
        self.queues = {}
        self.current_tracks = {}

    def get_guild_queue(self, gid):
        if gid not in self.queues:
            self.queues[gid] = []
        return self.queues[gid]

    async def play_next(self, ctx, gid):
        queue = self.get_guild_queue(gid)
        if queue:
            volume = self.volumes.get(gid, 1.0)
            next_track = queue.pop(0)
            if ctx.guild.voice_client:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(next_track["url"], **FFMPEG_OPTIONS),
                    volume=volume,
                )
                ctx.guild.voice_client.play(
                    source,
                    after=lambda e: self.bot.loop.create_task(self.play_next(ctx, gid)),
                )
                self.current_tracks[gid] = next_track
                await ctx.send(f"Now playing: {next_track['title']}")
        else:
            self.current_tracks[gid] = None

    @commands.command(name="join", help="Bot joins the voice channel you are in.")
    async def join(self, ctx):
        if not ctx.author.voice:
            await ctx.send("You are not connected to a voice channel.")
            return
        channel = ctx.author.voice.channel
        if ctx.voice_client:
            try:
                await ctx.voice_client.move_to(channel)
                await ctx.send(f"Moved to {channel.name}!")
            except Exception as e:
                await ctx.send(f"Error moving voice channel: {e}")
                print(f"Error moving voice channel: {e}")
        else:
            try:
                await channel.connect()
                await ctx.send(f"Joined {channel.name}!")
            except Exception as e:
                await ctx.send(f"Error connecting to voice channel: {e}")
                print(f"Error connecting to voice channel: {e}")

    @commands.command(name="play", help="Plays audio from a YouTube link or playlist.")
    async def play(self, ctx, url: str):
        if not ctx.voice_client:
            if ctx.author.voice:
                try:
                    await ctx.author.voice.channel.connect()
                except Exception as e:
                    await ctx.send(f"Error joining voice channel: {e}")
                    print(f"Error joining voice channel: {e}")
                    return
            else:
                await ctx.send("You must be in a voice channel to play audio.")
                return
        with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                await ctx.send(f"Error processing the URL: {e}")
                print(f"Error processing URL: {e}")
                return
        gid = str(ctx.guild.id)
        queue = self.get_guild_queue(gid)
        volume = self.volumes.get(gid, 1.0)
        if "entries" in info:
            entries = info["entries"]
            if not entries:
                await ctx.send("No videos found in the playlist.")
                return
            if not ctx.voice_client.is_playing():
                first_entry = entries[0]
                audio_url = first_entry.get("url", first_entry["formats"][0]["url"])
                track = {
                    "url": audio_url,
                    "title": first_entry.get("title", "Unknown Title"),
                }
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTIONS),
                    volume=volume,
                )
                ctx.voice_client.play(
                    source,
                    after=lambda e: self.bot.loop.create_task(self.play_next(ctx, gid)),
                )
                self.current_tracks[gid] = track
                await ctx.send(f"Now playing: {track['title']}")
                for entry in entries[1:]:
                    audio_url = entry.get("url", entry["formats"][0]["url"])
                    queue.append(
                        {"url": audio_url, "title": entry.get("title", "Unknown Title")}
                    )
                await ctx.send(f"Added {len(entries) - 1} tracks to the queue.")
            else:
                for entry in entries:
                    audio_url = entry.get("url", entry["formats"][0]["url"])
                    queue.append(
                        {"url": audio_url, "title": entry.get("title", "Unknown Title")}
                    )
                await ctx.send(f"Added playlist to queue with {len(entries)} tracks.")
        else:
            audio_url = info.get("url", info["formats"][0]["url"])
            track = {"url": audio_url, "title": info.get("title", "Unknown Title")}
            if ctx.voice_client.is_playing():
                queue.append(track)
                await ctx.send(f"Added to queue: {track['title']}")
            else:
                source = discord.PCMVolumeTransformer(
                    discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTIONS),
                    volume=volume,
                )
                ctx.voice_client.play(
                    source,
                    after=lambda e: self.bot.loop.create_task(self.play_next(ctx, gid)),
                )
                self.current_tracks[gid] = track
                await ctx.send(f"Now playing: {track['title']}")

    @commands.command(
        name="search", help="Search YouTube for a track and choose one to play."
    )
    async def search(self, ctx, *, query: str):
        if not ctx.voice_client:
            if ctx.author.voice:
                try:
                    await ctx.author.voice.channel.connect()
                except Exception as e:
                    await ctx.send(f"Error joining voice channel: {e}")
                    print(f"Error joining voice channel: {e}")
                    return
            else:
                await ctx.send("You must be in a voice channel to play audio.")
                return
        gid = str(ctx.guild.id)
        with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
            try:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
            except Exception as e:
                await ctx.send(f"Error processing search: {e}")
                print(f"Error processing search: {e}")
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
        except:
            await ctx.send("Selection timed out.")
            return
        selection = int(reply.content)
        selected_entry = entries[selection - 1]
        with youtube_dl.YoutubeDL(YDL_OPTIONS) as ydl:
            try:
                video_info = ydl.extract_info(
                    selected_entry["webpage_url"], download=False
                )
            except Exception as e:
                await ctx.send(f"Error processing the selected video: {e}")
                print(f"Error processing selected video: {e}")
                return
        audio_url = video_info.get("url", video_info["formats"][0]["url"])
        track = {"url": audio_url, "title": video_info.get("title", "Unknown Title")}
        volume = self.volumes.get(gid, 1.0)
        queue = self.get_guild_queue(gid)
        if ctx.voice_client.is_playing():
            queue.append(track)
            await ctx.send(f"Added to queue: {track['title']}")
        else:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track["url"], **FFMPEG_OPTIONS),
                volume=volume,
            )
            ctx.voice_client.play(
                source,
                after=lambda e: self.bot.loop.create_task(self.play_next(ctx, gid)),
            )
            self.current_tracks[gid] = track
            await ctx.send(f"Now playing: {track['title']}")

    @commands.command(name="queue", help="Lists the next 10 songs in the queue.")
    async def queue_list(self, ctx):
        gid = str(ctx.guild.id)
        queue = self.queues.get(gid, [])
        if not queue:
            await ctx.send("The queue is empty.")
            return
        msg = f"Queue: {len(queue)} song(s)\nUpcoming tracks:\n"
        for i, track in enumerate(queue[:10], start=1):
            msg += f"{i}. {track['title']}\n"
        await ctx.send(msg)

    @commands.command(name="np", help="Shows the currently playing track.")
    async def np(self, ctx):
        gid = str(ctx.guild.id)
        current = self.current_tracks.get(gid)
        if current:
            await ctx.send(f"Now playing: {current['title']}")
        else:
            await ctx.send("No track is currently playing.")

    @commands.command(name="skip", help="Skips the current track.")
    async def skip(self, ctx):
        if ctx.voice_client:
            ctx.voice_client.stop()
            await ctx.send("Track skipped.")
        else:
            await ctx.send("I'm not connected to a voice channel.")

    @commands.command(
        name="remove",
        help="Removes tracks from the queue. Usage: !remove [1-10|first|last|all|search text]",
    )
    async def remove(self, ctx, arg: str):
        gid = str(ctx.guild.id)
        queue = self.get_guild_queue(gid)
        if not queue:
            await ctx.send("The queue is empty.")
            return
        arg_lower = arg.lower()
        if arg_lower == "first":
            removed = queue.pop(0)
            await ctx.send(f"Removed track: {removed.get('title', 'Unknown Title')}")
        elif arg_lower == "last":
            removed = queue.pop(-1)
            await ctx.send(f"Removed track: {removed.get('title', 'Unknown Title')}")
        elif arg_lower == "all":
            count = len(queue)
            self.queues[gid] = []
            await ctx.send(f"Cleared the queue. Removed {count} track(s).")
        else:
            try:
                index = int(arg)
                if index < 1 or index > 10:
                    await ctx.send("Index must be between 1 and 10.")
                    return
                if index > len(queue):
                    await ctx.send("There are not that many tracks in the queue.")
                    return
                removed = queue.pop(index - 1)
                await ctx.send(
                    f"Removed track: {removed.get('title', 'Unknown Title')}"
                )
            except ValueError:
                search_term = arg_lower
                found = None
                for i, track in enumerate(queue):
                    if search_term in track.get("title", "").lower():
                        found = (i, track)
                        break
                if found is not None:
                    index, removed = found
                    queue.pop(index)
                    await ctx.send(
                        f"Removed track: {removed.get('title', 'Unknown Title')}"
                    )
                else:
                    await ctx.send("No track found matching that title.")

    @commands.command(
        name="volume",
        help="Sets the playback volume as a percentage and saves it for this server.",
    )
    async def volume(self, ctx, vol: int):
        if vol < 0 or vol > 150:
            await ctx.send("Volume must be between 0 and 150.")
            return
        gid = str(ctx.guild.id)
        self.volumes[gid] = vol / 100
        with open(self.volumes_file, "w") as f:
            json.dump(self.volumes, f)
        if (
            ctx.voice_client
            and ctx.voice_client.source
            and isinstance(ctx.voice_client.source, discord.PCMVolumeTransformer)
        ):
            ctx.voice_client.source.volume = self.volumes[gid]
        await ctx.send(f"Volume set to {vol}% for this server.")

    @commands.command(name="stop", help="Stops playback and disconnects the bot.")
    async def stop(self, ctx):
        if ctx.voice_client:
            try:
                await ctx.voice_client.disconnect()
                gid = str(ctx.guild.id)
                self.queues[gid] = []
                self.current_tracks[gid] = None
                await ctx.send("Stopped playback and disconnected.")
            except Exception as e:
                await ctx.send(f"Error disconnecting: {e}")
                print(f"Error disconnecting: {e}")
        else:
            await ctx.send("I'm not connected to a voice channel.")

    @commands.command(name="pause", help="Pauses the current track.")
    async def pause(self, ctx):
        if ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("Music has been paused.")
        else:
            await ctx.send("No music is currently playing.")

    @commands.command(name="resume", help="Resumes the paused track.")
    async def resume(self, ctx):
        if ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("Music has been resumed.")
        else:
            await ctx.send("Music is not paused.")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.send("You do not have permission to use this command.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(MusicCommands(bot))
