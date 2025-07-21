# https://developers.google.com/youtube/v3/getting-started
import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
from datetime import datetime, timedelta
import json
import os
from cogs.admin import is_admin
from logger import get_logger


class Notifications(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = get_logger()

        # Load API keys from config
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(script_dir, "config.json")) as f:
            config = json.load(f)

        self.youtube_api_key = config.get("youtube_api_key")
        self.twitch_client_id = config.get("twitch_client_id")
        self.twitch_client_secret = config.get("twitch_client_secret")
        self.twitch_token = None

        # Start monitoring tasks
        if self.youtube_api_key:
            self.check_youtube.start()
        if self.twitch_client_id and self.twitch_client_secret:
            self.check_twitch.start()

    def cog_unload(self):
        """Clean up when cog is unloaded"""
        self.check_youtube.cancel()
        self.check_twitch.cancel()

    async def get_twitch_token(self):
        """Get OAuth token for Twitch API"""
        if self.twitch_token:
            return self.twitch_token

        async with aiohttp.ClientSession() as session:
            data = {
                "client_id": self.twitch_client_id,
                "client_secret": self.twitch_client_secret,
                "grant_type": "client_credentials",
            }

            async with session.post(
                "https://id.twitch.tv/oauth2/token", data=data
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    self.twitch_token = result["access_token"]
                    return self.twitch_token
                else:
                    self.logger.error(f"Failed to get Twitch token: {resp.status}")
                    return None

    @tasks.loop(minutes=5)
    async def check_youtube(self):
        """Check for new YouTube videos"""
        try:
            from db import get_youtube_subscriptions, mark_video_notified

            subscriptions = await get_youtube_subscriptions()

            for sub in subscriptions:
                guild_id = sub["guild_id"]
                channel_id = sub["channel_id"]
                youtube_channel_id = sub["youtube_channel_id"]
                notification_channel_id = sub["notification_channel_id"]
                last_checked = sub.get(
                    "last_checked", datetime.utcnow() - timedelta(hours=1)
                )

                # Get recent videos from YouTube channel
                videos = await self._get_youtube_videos(
                    youtube_channel_id, last_checked
                )

                if videos:
                    notification_channel = self.bot.get_channel(
                        int(notification_channel_id)
                    )
                    if notification_channel:
                        for video in videos:
                            # Check if already notified
                            from db import is_video_notified

                            if not await is_video_notified(video["id"]):
                                await self._send_youtube_notification(
                                    notification_channel, video
                                )
                                await mark_video_notified(video["id"])

                # Update last checked time
                from db import update_youtube_last_checked

                await update_youtube_last_checked(guild_id, youtube_channel_id)

        except Exception as e:
            self.logger.error(f"Error checking YouTube: {e}")

    @tasks.loop(minutes=2)
    async def check_twitch(self):
        """Check for live Twitch streams"""
        try:
            from db import (
                get_twitch_subscriptions,
                get_stream_status,
                update_stream_status,
            )

            subscriptions = await get_twitch_subscriptions()

            if not subscriptions:
                return

            # Get all streamers to check
            streamers = list(set([sub["twitch_username"] for sub in subscriptions]))

            # Check stream status in batches of 100 (Twitch API limit)
            for i in range(0, len(streamers), 100):
                batch = streamers[i : i + 100]
                streams = await self._get_twitch_streams(batch)

                for sub in subscriptions:
                    username = sub["twitch_username"]
                    guild_id = sub["guild_id"]
                    notification_channel_id = sub["notification_channel_id"]

                    # Check if streamer is currently live
                    current_stream = next(
                        (s for s in streams if s["user_login"] == username.lower()),
                        None,
                    )
                    was_live = await get_stream_status(guild_id, username)

                    notification_channel = self.bot.get_channel(
                        int(notification_channel_id)
                    )

                    if current_stream and not was_live:
                        # Stream went live
                        if notification_channel:
                            await self._send_twitch_notification(
                                notification_channel, current_stream, "live"
                            )
                        await update_stream_status(
                            guild_id, username, True, current_stream["id"]
                        )
                    elif not current_stream and was_live:
                        # Stream went offline
                        if notification_channel:
                            await self._send_twitch_notification(
                                notification_channel, {"user_name": username}, "offline"
                            )
                        await update_stream_status(guild_id, username, False, None)

        except Exception as e:
            self.logger.error(f"Error checking Twitch: {e}")

    async def _get_youtube_videos(self, channel_id, since):
        """Get recent YouTube videos from a channel"""
        if not self.youtube_api_key:
            return []

        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        async with aiohttp.ClientSession() as session:
            # First get channel uploads playlist
            url = f"https://www.googleapis.com/youtube/v3/channels"
            params = {
                "key": self.youtube_api_key,
                "id": channel_id,
                "part": "contentDetails",
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                if not data.get("items"):
                    return []

                uploads_playlist = data["items"][0]["contentDetails"][
                    "relatedPlaylists"
                ]["uploads"]

            # Get recent videos from uploads playlist
            url = f"https://www.googleapis.com/youtube/v3/playlistItems"
            params = {
                "key": self.youtube_api_key,
                "playlistId": uploads_playlist,
                "part": "snippet",
                "maxResults": 10,
                "order": "date",
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []

                data = await resp.json()
                videos = []

                for item in data.get("items", []):
                    published = datetime.fromisoformat(
                        item["snippet"]["publishedAt"].replace("Z", "+00:00")
                    )
                    if published > since:
                        videos.append(
                            {
                                "id": item["snippet"]["resourceId"]["videoId"],
                                "title": item["snippet"]["title"],
                                "url": f"https://www.youtube.com/watch?v={item['snippet']['resourceId']['videoId']}",
                                "thumbnail": item["snippet"]["thumbnails"]["medium"][
                                    "url"
                                ],
                                "channel_name": item["snippet"]["channelTitle"],
                                "published_at": published,
                            }
                        )

                return videos

    async def _get_twitch_streams(self, usernames):
        """Get live stream info for multiple Twitch usernames"""
        token = await self.get_twitch_token()
        if not token:
            return []

        async with aiohttp.ClientSession() as session:
            headers = {
                "Client-ID": self.twitch_client_id,
                "Authorization": f"Bearer {token}",
            }

            # Convert usernames to user IDs first
            url = "https://api.twitch.tv/helix/users"
            params = {"login": usernames}

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    return []

                users_data = await resp.json()
                user_ids = [user["id"] for user in users_data.get("data", [])]

            if not user_ids:
                return []

            # Get stream info
            url = "https://api.twitch.tv/helix/streams"
            params = {"user_id": user_ids}

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    return []

                streams_data = await resp.json()
                return streams_data.get("data", [])

    async def _send_youtube_notification(self, channel, video):
        """Send YouTube notification to Discord channel"""
        embed = discord.Embed(
            title=video["title"],
            url=video["url"],
            color=0xFF0000,
            timestamp=video["published_at"],
        )
        embed.set_author(name=f"{video['channel_name']} uploaded a new video!")
        embed.set_thumbnail(url=video["thumbnail"])
        embed.set_footer(text="YouTube")

        await channel.send(embed=embed)

    async def _send_twitch_notification(self, channel, stream_data, status):
        """Send Twitch notification to Discord channel"""
        if status == "live":
            embed = discord.Embed(
                title=stream_data.get("title", "Live Stream"),
                url=f"https://twitch.tv/{stream_data['user_login']}",
                color=0x9146FF,
            )
            embed.set_author(name=f"{stream_data['user_name']} is now live on Twitch!")
            embed.add_field(
                name="Game", value=stream_data.get("game_name", "Unknown"), inline=True
            )
            embed.add_field(
                name="Viewers", value=stream_data.get("viewer_count", 0), inline=True
            )

            if stream_data.get("thumbnail_url"):
                thumbnail = (
                    stream_data["thumbnail_url"]
                    .replace("{width}", "320")
                    .replace("{height}", "180")
                )
                embed.set_image(url=thumbnail)

            embed.set_footer(text="Twitch")
        else:
            embed = discord.Embed(title="Stream Ended", color=0x9146FF)
            embed.set_author(name=f"{stream_data['user_name']} has gone offline")
            embed.set_footer(text="Twitch")

        await channel.send(embed=embed)

    @commands.group(
        name="notifications",
        aliases=["notif"],
        help="Manage YouTube and Twitch notifications",
    )
    @is_admin()
    async def notifications(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @notifications.command(
        name="channel", help="Set the notification channel for this server"
    )
    @is_admin()
    async def set_notification_channel(self, ctx, channel: discord.TextChannel):
        """Set the notification channel for this server"""
        from db import set_notification_channel

        await set_notification_channel(str(ctx.guild.id), str(channel.id))
        await ctx.send(f"✅ Notification channel set to {channel.mention}")

    @notifications.group(
        name="youtube", aliases=["yt"], help="Manage YouTube subscriptions"
    )
    @is_admin()
    async def youtube(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @youtube.command(name="add", help="Add a YouTube channel to track")
    @is_admin()
    async def youtube_add(self, ctx, channel_id: str):
        """Add YouTube channel subscription"""
        if not self.youtube_api_key:
            await ctx.send("❌ YouTube API key not configured!")
            return

        from db import add_youtube_subscription, get_notification_channel

        # Check if notification channel is set
        notif_channel_id = await get_notification_channel(str(ctx.guild.id))
        if not notif_channel_id:
            await ctx.send(
                "❌ Please set a notification channel first with `!notifications channel #channel`"
            )
            return

        # Validate YouTube channel
        async with aiohttp.ClientSession() as session:
            url = f"https://www.googleapis.com/youtube/v3/channels"
            params = {"key": self.youtube_api_key, "id": channel_id, "part": "snippet"}

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    await ctx.send("❌ Invalid YouTube channel ID or API error!")
                    return

                data = await resp.json()
                if not data.get("items"):
                    await ctx.send("❌ YouTube channel not found!")
                    return

                channel_name = data["items"][0]["snippet"]["title"]

        try:
            await add_youtube_subscription(
                str(ctx.guild.id), channel_id, notif_channel_id
            )
            await ctx.send(f"✅ Added YouTube channel **{channel_name}** to tracking!")
        except ValueError as e:
            await ctx.send(f"❌ {str(e)}")

    @youtube.command(name="remove", help="Remove a YouTube channel from tracking")
    @is_admin()
    async def youtube_remove(self, ctx, channel_id: str):
        """Remove YouTube channel subscription"""
        from db import remove_youtube_subscription

        if await remove_youtube_subscription(str(ctx.guild.id), channel_id):
            await ctx.send("✅ Removed YouTube channel from tracking!")
        else:
            await ctx.send("❌ YouTube channel not found in subscriptions!")

    @youtube.command(name="list", help="List all tracked YouTube channels")
    @is_admin()
    async def youtube_list(self, ctx):
        """List YouTube subscriptions"""
        from db import get_youtube_subscriptions_by_guild

        subscriptions = await get_youtube_subscriptions_by_guild(str(ctx.guild.id))

        if not subscriptions:
            await ctx.send("📭 No YouTube channels being tracked.")
            return

        embed = discord.Embed(title="YouTube Subscriptions", color=0xFF0000)

        for sub in subscriptions[:10]:  # Limit to 10 for embed limits
            embed.add_field(
                name="Channel ID", value=sub["youtube_channel_id"], inline=False
            )

        if len(subscriptions) > 10:
            embed.set_footer(text=f"Showing 10 of {len(subscriptions)} subscriptions")

        await ctx.send(embed=embed)

    @notifications.group(name="twitch", help="Manage Twitch subscriptions")
    @is_admin()
    async def twitch(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @twitch.command(name="add", help="Add a Twitch streamer to track")
    @is_admin()
    async def twitch_add(self, ctx, username: str):
        """Add Twitch streamer subscription"""
        if not self.twitch_client_id:
            await ctx.send("❌ Twitch API not configured!")
            return

        from db import add_twitch_subscription, get_notification_channel

        # Check if notification channel is set
        notif_channel_id = await get_notification_channel(str(ctx.guild.id))
        if not notif_channel_id:
            await ctx.send(
                "❌ Please set a notification channel first with `!notifications channel #channel`"
            )
            return

        # Validate Twitch username
        token = await self.get_twitch_token()
        if not token:
            await ctx.send("❌ Failed to authenticate with Twitch API!")
            return

        async with aiohttp.ClientSession() as session:
            headers = {
                "Client-ID": self.twitch_client_id,
                "Authorization": f"Bearer {token}",
            }

            url = "https://api.twitch.tv/helix/users"
            params = {"login": username.lower()}

            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    await ctx.send("❌ Error checking Twitch username!")
                    return

                data = await resp.json()
                if not data.get("data"):
                    await ctx.send("❌ Twitch user not found!")
                    return

                display_name = data["data"][0]["display_name"]

        try:
            await add_twitch_subscription(
                str(ctx.guild.id), username.lower(), notif_channel_id
            )
            await ctx.send(f"✅ Added Twitch streamer **{display_name}** to tracking!")
        except ValueError as e:
            await ctx.send(f"❌ {str(e)}")

    @twitch.command(name="remove", help="Remove a Twitch streamer from tracking")
    @is_admin()
    async def twitch_remove(self, ctx, username: str):
        """Remove Twitch streamer subscription"""
        from db import remove_twitch_subscription

        if await remove_twitch_subscription(str(ctx.guild.id), username.lower()):
            await ctx.send(f"✅ Removed Twitch streamer **{username}** from tracking!")
        else:
            await ctx.send("❌ Twitch streamer not found in subscriptions!")

    @twitch.command(name="list", help="List all tracked Twitch streamers")
    @is_admin()
    async def twitch_list(self, ctx):
        """List Twitch subscriptions"""
        from db import get_twitch_subscriptions_by_guild

        subscriptions = await get_twitch_subscriptions_by_guild(str(ctx.guild.id))

        if not subscriptions:
            await ctx.send("📭 No Twitch streamers being tracked.")
            return

        embed = discord.Embed(title="Twitch Subscriptions", color=0x9146FF)

        streamers = "\n".join([sub["twitch_username"] for sub in subscriptions[:20]])
        embed.add_field(name="Streamers", value=streamers, inline=False)

        if len(subscriptions) > 20:
            embed.set_footer(text=f"Showing 20 of {len(subscriptions)} subscriptions")

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Notifications(bot))
