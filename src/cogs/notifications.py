import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import aiohttp
import discord
from discord.ext import commands, tasks

from command_help import apply_parameter_descriptions, describe_parameters
from cogs.admin import is_admin
from db import (
    add_twitch_subscription,
    add_youtube_subscription,
    get_database_service,
    get_notification_channel,
    get_twitch_subscriptions_by_guild,
    get_youtube_subscriptions_by_guild,
    remove_twitch_subscription,
    remove_youtube_subscription,
    set_notification_channel,
)
from logger import get_logger
from twitch_transitions import (
    TwitchDeliveryState,
    TwitchStream,
    TwitchSubscription,
    TwitchTransitions,
)
from youtube_notification_delivery import (
    PublicationKind,
    YouTubeNotificationDelivery,
    YouTubePublication,
    YouTubeSubscription,
)
from youtube_platform import YouTubeApiAdapter


class TwitchApiAdapter:
    def __init__(self, fetch_streams, fetch_vod_url):
        self._fetch_streams = fetch_streams
        self._fetch_vod_url = fetch_vod_url

    async def live_streams(self, usernames: Sequence[str]):
        streams = await self._fetch_streams(usernames)
        if streams is None:
            return None
        return [
            TwitchStream(
                stream_id=stream["id"],
                user_id=stream.get("user_id"),
                user_login=stream["user_login"],
                display_name=stream.get("user_name"),
                payload=stream,
            )
            for stream in streams
        ]

    async def vod_url(
        self,
        user_id: Optional[str],
        stream_id: Optional[str],
        user_login: Optional[str],
    ) -> Optional[str]:
        return await self._fetch_vod_url(user_id, stream_id, user_login)


class DiscordTwitchPublisher:
    def __init__(self, bot, send_notification):
        self._bot = bot
        self._send_notification = send_notification

    async def publish_live(
        self, subscription: TwitchSubscription, stream: TwitchStream
    ) -> str:
        channel = self._notification_channel(subscription)
        return await self._send_notification(channel, dict(stream.payload), "live")

    async def publish_offline(
        self,
        subscription: TwitchSubscription,
        state: TwitchDeliveryState,
        vod_url: Optional[str],
    ) -> str:
        channel = self._notification_channel(subscription)
        payload = {
            "user_name": state.display_name or state.twitch_username,
            "display_name": state.display_name or state.twitch_username,
            "user_login": state.user_login or state.twitch_username,
            "user_id": state.user_id,
            "stream_id": state.stream_id,
            "message_id": state.message_id,
        }
        return await self._send_notification(channel, payload, "offline", vod_url)

    def _notification_channel(self, subscription: TwitchSubscription):
        channel = self._bot.get_channel(int(subscription.notification_channel_id))
        if not channel:
            raise LookupError(
                f"Notification channel {subscription.notification_channel_id} is unavailable"
            )
        return channel


class MongoYouTubeNotificationStore:
    def __init__(self, database):
        self._database = database

    async def tracked_subscriptions(self):
        return await self._database.tracked_youtube_subscriptions()

    async def observation_cursor(self, channel_id):
        return await self._database.youtube_observation_cursor(channel_id)

    async def record_observation(self, channel_id, cursor, deliveries):
        await self._database.record_youtube_observation(
            channel_id, cursor, deliveries
        )

    async def pending_deliveries(self):
        return await self._database.pending_youtube_deliveries()

    async def save_delivery(self, delivery):
        await self._database.save_youtube_delivery(delivery)


class DiscordYouTubePublisher:
    _TYPE_LABELS = {
        PublicationKind.LIVE: "Live",
        PublicationKind.UPCOMING_STREAM: "Upcoming stream",
        PublicationKind.ARCHIVED_STREAM: "Archived stream",
        PublicationKind.VIDEO: "Video",
    }
    _AUTHOR_ACTIONS = {
        PublicationKind.LIVE: "is live!",
        PublicationKind.UPCOMING_STREAM: "scheduled a stream!",
        PublicationKind.ARCHIVED_STREAM: "published a stream recording!",
        PublicationKind.VIDEO: "uploaded a new video!",
    }

    def __init__(self, bot):
        self._bot = bot

    async def publish(
        self, subscription: YouTubeSubscription, publication: YouTubePublication
    ) -> str:
        channel = self._bot.get_channel(int(subscription.notification_channel_id))
        if channel is None:
            raise RuntimeError(
                f"Discord channel {subscription.notification_channel_id} is unavailable"
            )
        embed = discord.Embed(
            title=publication.title,
            url=publication.url,
            color=0xFF0000,
            timestamp=publication.published_at,
        )
        embed.set_author(
            name=f"{publication.channel_name} {self._AUTHOR_ACTIONS[publication.kind]}"
        )
        embed.add_field(
            name="Type", value=self._TYPE_LABELS[publication.kind], inline=True
        )
        if publication.thumbnail_url:
            embed.set_thumbnail(url=publication.thumbnail_url)
        embed.set_footer(text="YouTube")
        message = await channel.send(embed=embed)
        return str(message.id)


class Notifications(commands.Cog):
    """YouTube and Twitch notifications with per-subscription channels."""

    def __init__(self, bot):
        self.bot = bot
        apply_parameter_descriptions(self)
        self.logger = get_logger()

        # Load API keys (env first, fallback to config.json for local dev)
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(script_dir, "config.json")
        config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    config = json.load(f)
            except Exception as e:
                self.logger.warning(f"Failed to load config.json for notifications: {e}")

        self.youtube_api_key = os.getenv("YOUTUBE_API_KEY") or config.get(
            "youtube_api_key"
        )
        self.twitch_client_id = os.getenv("TWITCH_CLIENT_ID") or config.get(
            "twitch_client_id"
        )
        self.twitch_client_secret = os.getenv("TWITCH_CLIENT_SECRET") or config.get(
            "twitch_client_secret"
        )
        self.twitch_token: Optional[str] = None
        database = get_database_service()
        self.youtube_delivery = (
            YouTubeNotificationDelivery(
                YouTubeApiAdapter(self.youtube_api_key, logger=self.logger),
                MongoYouTubeNotificationStore(database),
                DiscordYouTubePublisher(bot),
                logger=self.logger,
            )
            if self.youtube_api_key
            else None
        )
        self.twitch_transitions = TwitchTransitions(
            TwitchApiAdapter(self._get_twitch_streams, self._get_twitch_vod_url),
            database,
            DiscordTwitchPublisher(bot, self._send_twitch_notification),
            logger=self.logger,
        )

        if self.youtube_api_key:
            self.check_youtube.start()
            self.logger.info("[Notifications] YouTube tracking enabled (checking every 5 minutes)")
        else:
            self.logger.warning("[Notifications] YouTube tracking disabled - no API key configured")

        if self.twitch_client_id and self.twitch_client_secret:
            self.check_twitch.start()
            self.logger.info("[Notifications] Twitch tracking enabled (checking every 2 minutes)")
        else:
            self.logger.warning("[Notifications] Twitch tracking disabled - no API credentials configured")

    def cog_unload(self):
        if self.check_youtube.is_running():
            self.check_youtube.cancel()
        if self.check_twitch.is_running():
            self.check_twitch.cancel()

    # --- Background tasks -------------------------------------------------
    @tasks.loop(minutes=5)
    async def check_youtube(self):
        """Poll YouTube subscriptions for new uploads."""
        try:
            if self.youtube_delivery:
                await self.youtube_delivery.poll()
        except Exception as e:
            self.logger.error(f"[YouTube] Error in check loop: {e}", exc_info=True)

    @tasks.loop(minutes=2)
    async def check_twitch(self):
        """Poll Twitch subscriptions for live/offline transitions."""
        try:
            await self.twitch_transitions.poll()
        except Exception as e:
            self.logger.error(f"[Twitch] Error in check loop: {e}", exc_info=True)

    # --- External API helpers --------------------------------------------
    async def _get_twitch_streams(self, usernames: Sequence[str]):
        """Fetch live stream info for multiple Twitch usernames."""
        token = await self.get_twitch_token()
        if not token:
            self.logger.error("[Twitch API] No token available")
            return None

        async with aiohttp.ClientSession() as session:
            headers = {
                "Client-ID": self.twitch_client_id,
                "Authorization": f"Bearer {token}",
            }

            # Step 1: Get user IDs from usernames
            url = "https://api.twitch.tv/helix/users"
            params = {"login": usernames}
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 401:
                    self.logger.warning("[Twitch API] Token expired, clearing cached token")
                    self.twitch_token = None
                    return None
                if resp.status != 200:
                    response_text = await resp.text()
                    self.logger.error(
                        f"[Twitch API] Failed to get users: HTTP {resp.status} - {response_text[:500]}"
                    )
                    return None
                users_data = await resp.json()
                user_ids = [user["id"] for user in users_data.get("data", [])]
                self.logger.debug(
                    f"[Twitch API] Resolved {len(user_ids)} user IDs from {len(usernames)} usernames"
                )

            if not user_ids:
                self.logger.warning(f"[Twitch API] No user IDs found for usernames: {usernames}")
                return []

            # Step 2: Get streams for those user IDs
            url = "https://api.twitch.tv/helix/streams"
            params = {"user_id": user_ids}
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    response_text = await resp.text()
                    self.logger.error(
                        f"[Twitch API] Failed to get streams: HTTP {resp.status} - {response_text[:500]}"
                    )
                    return None
                streams_data = await resp.json()
                streams = streams_data.get("data", [])
                self.logger.debug(f"[Twitch API] Found {len(streams)} live stream(s)")
                return streams

    async def _get_twitch_vod_url(
        self, user_id: Optional[str], stream_id: Optional[str], user_login: Optional[str]
    ) -> Optional[str]:
        """Return the VOD URL for the most recent stream, preferring a VOD that matches the stream_id."""
        token = await self.get_twitch_token()
        if not token:
            self.logger.warning("[Twitch API] Cannot get VOD - no token available")
            return None

        # Resolve user_id if we only have the login.
        resolved_user_id = user_id
        if not resolved_user_id and user_login:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "Client-ID": self.twitch_client_id,
                    "Authorization": f"Bearer {token}",
                }
                url = "https://api.twitch.tv/helix/users"
                params = {"login": user_login}
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status != 200:
                        self.logger.warning(
                            f"[Twitch API] Failed to resolve user_id for {user_login}: HTTP {resp.status}"
                        )
                        return None
                    data = await resp.json()
                    if not data.get("data"):
                        self.logger.warning(f"[Twitch API] No user data found for {user_login}")
                        return None
                    resolved_user_id = data["data"][0]["id"]

        if not resolved_user_id:
            self.logger.warning("[Twitch API] Cannot get VOD - no user_id available")
            return None

        async with aiohttp.ClientSession() as session:
            headers = {
                "Client-ID": self.twitch_client_id,
                "Authorization": f"Bearer {token}",
            }
            params = {"user_id": resolved_user_id, "type": "archive", "first": 5}
            async with session.get(
                "https://api.twitch.tv/helix/videos", headers=headers, params=params
            ) as resp:
                if resp.status != 200:
                    self.logger.warning(
                        f"[Twitch API] Failed to get VODs for user {resolved_user_id}: HTTP {resp.status}"
                    )
                    return None
                data = await resp.json()

        videos = data.get("data", [])
        self.logger.debug(f"[Twitch API] Found {len(videos)} VOD(s) for user {resolved_user_id}")

        for video in videos:
            if stream_id and video.get("stream_id") == stream_id:
                self.logger.debug(f"[Twitch API] Found matching VOD for stream {stream_id}: {video.get('url')}")
                return video.get("url")

        if videos:
            self.logger.debug(f"[Twitch API] Using most recent VOD: {videos[0].get('url')}")
            return videos[0].get("url")

        self.logger.debug(f"[Twitch API] No VODs found for user {resolved_user_id}")
        return None

    async def get_twitch_token(self):
        """Get OAuth token for Twitch API (cached until process restarts)."""
        if self.twitch_token:
            return self.twitch_token

        self.logger.info("[Twitch API] Requesting new OAuth token")
        async with aiohttp.ClientSession() as session:
            data = {
                "client_id": self.twitch_client_id,
                "client_secret": self.twitch_client_secret,
                "grant_type": "client_credentials",
            }
            async with session.post("https://id.twitch.tv/oauth2/token", data=data) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    self.twitch_token = result["access_token"]
                    self.logger.info("[Twitch API] Successfully obtained OAuth token")
                    return self.twitch_token
                response_text = await resp.text()
                self.logger.error(
                    f"[Twitch API] Failed to get token: HTTP {resp.status} - {response_text[:500]}"
                )
                return None

    async def _resolve_youtube_channel(
        self, raw_identifier: str
    ) -> Optional[tuple[str, str]]:
        """Resolve a YouTube channel ID from ID, URL, or @handle and return (id, title)."""
        ident = raw_identifier.strip()
        channel_id: Optional[str] = None
        handle_query: Optional[str] = None

        if ident.startswith("http"):
            try:
                # crude parse without urlparse dependency
                path = ident.split("youtube.com")[-1]
                parts = [p for p in path.split("/") if p]
                if len(parts) >= 2 and parts[0] == "channel":
                    channel_id = parts[1]
                elif parts and parts[0].startswith("@"):
                    handle_query = parts[0].lstrip("@")
                elif len(parts) >= 2 and parts[0] in ("c", "user"):
                    handle_query = parts[1]
            except Exception:
                pass
        elif ident.startswith("@"):
            handle_query = ident[1:]
        else:
            channel_id = ident

        # If we have an explicit ID, try to fetch it directly
        if channel_id:
            data = await self._fetch_channel_snippet_by_id(channel_id)
            if data:
                return channel_id, data["snippet"]["title"]

        # Otherwise, attempt to resolve via search (handles or custom names)
        if handle_query:
            search_result = await self._search_channel(handle_query)
            if search_result:
                return search_result["id"], search_result["title"]

        return None

    async def _fetch_channel_snippet_by_id(self, channel_id: str) -> Optional[dict]:
        async with aiohttp.ClientSession() as session:
            url = "https://www.googleapis.com/youtube/v3/channels"
            params = {"key": self.youtube_api_key, "id": channel_id, "part": "snippet"}
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items")
                if not items:
                    return None
                return items[0]

    async def _search_channel(self, query: str) -> Optional[dict]:
        async with aiohttp.ClientSession() as session:
            url = "https://www.googleapis.com/youtube/v3/search"
            params = {
                "key": self.youtube_api_key,
                "q": query,
                "type": "channel",
                "part": "snippet",
                "maxResults": 1,
            }
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items")
                if not items:
                    return None
                item = items[0]
                return {
                    "id": item["snippet"]["channelId"],
                    "title": item["snippet"]["title"],
                }

    async def _send_twitch_notification(
        self,
        channel: discord.TextChannel,
        stream_data: dict,
        status: str,
        vod_url: Optional[str] = None,
    ) -> Optional[str]:
        if status == "live":
            started_at = stream_data.get("started_at")
            timestamp = None
            if started_at:
                try:
                    timestamp = datetime.fromisoformat(
                        started_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    timestamp = None

            embed = discord.Embed(
                title=stream_data.get("title", "Live Stream"),
                url=f"https://twitch.tv/{stream_data['user_login']}",
                color=0x9146FF,
                timestamp=timestamp,
            )
            embed.set_author(name=f"{stream_data['user_name']} is now live on Twitch!")
            embed.description = f"[Watch Stream](https://twitch.tv/{stream_data['user_login']})"
            embed.add_field(
                name="Game", value=stream_data.get("game_name", "Unknown"), inline=True
            )
            embed.add_field(
                name="Viewers", value=stream_data.get("viewer_count", 0), inline=True
            )
            thumbnail = stream_data.get("thumbnail_url")
            if thumbnail:
                embed.set_image(
                    url=thumbnail.replace("{width}", "320").replace("{height}", "180")
                )
            embed.set_footer(text="Twitch")
            message = await channel.send(embed=embed)
            return str(message.id)

        user_login = (
            stream_data.get("user_login")
            or stream_data.get("username")
            or stream_data.get("user_name")
        )
        display_name = stream_data.get("display_name") or stream_data.get("user_name")
        link_target = vod_url or (f"https://twitch.tv/{user_login}" if user_login else None)
        link_label = "Watch VOD" if vod_url else "Visit Channel"

        embed = discord.Embed(
            title="Stream Ended",
            url=link_target,
            color=0x9146FF,
        )
        if display_name or user_login:
            embed.set_author(
                name=f"{display_name or user_login} has gone offline"
            )
        if link_target:
            embed.description = f"[{link_label}]({link_target})"
        embed.set_footer(text="Twitch")

        message_id = stream_data.get("message_id")
        if message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                await message.edit(embed=embed)
                return str(message.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError) as e:
                self.logger.warning(
                    f"Could not edit prior Twitch notification ({message_id}): {e}"
                )

        message = await channel.send(embed=embed)
        return str(message.id)

    # --- Commands ---------------------------------------------------------
    @commands.group(
        name="notifications",
        aliases=["notif"],
        help="Manage YouTube and Twitch notifications for this server.",
    )
    @is_admin()
    async def notifications(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @describe_parameters(channel="Text channel that receives notifications by default.")
    @notifications.command(
        name="channel",
        help="Set the default notification channel.\nUsage: !notifications channel #text-channel",
    )
    @is_admin()
    async def set_notification_channel_cmd(self, ctx, channel: discord.TextChannel):
        """Set the notification channel for this server."""
        await set_notification_channel(str(ctx.guild.id), str(channel.id))
        await ctx.send(f"Notification channel set to {channel.mention}")

    @notifications.group(
        name="youtube", aliases=["yt"], help="Manage YouTube subscriptions."
    )
    @is_admin()
    async def youtube(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @describe_parameters(
        channel_id="YouTube channel ID, channel URL, or @handle to track.",
        channel="Optional text channel for this subscription; otherwise use the default.",
    )
    @youtube.command(
        name="add",
        help="Add a YouTube channel.\nUsage: !notifications youtube add <channel_id|url|@handle> [#target-channel]",
    )
    @is_admin()
    async def youtube_add(
        self, ctx, channel_id: str, channel: Optional[discord.TextChannel] = None
    ):
        if not self.youtube_api_key:
            await ctx.send("YouTube API key not configured.")
            return

        target_channel_id = (
            str(channel.id) if channel else await get_notification_channel(str(ctx.guild.id))
        )
        if not target_channel_id:
            await ctx.send(
                "Set a default channel first with `!notifications channel #text-channel`, "
                "or pass one directly: `!notifications youtube add <channel_id> #text-channel`."
            )
            return

        resolved = await self._resolve_youtube_channel(channel_id)
        if not resolved:
            await ctx.send(
                "Could not resolve that channel. Use a channel ID, channel URL, or @handle."
            )
            return

        resolved_id, channel_name = resolved

        try:
            await add_youtube_subscription(
                str(ctx.guild.id), resolved_id, target_channel_id, channel_title=channel_name
            )
            target_text = channel.mention if channel else f"<#{target_channel_id}>"
            await ctx.send(
                f"Added YouTube channel **{channel_name}**. Notifications will post in {target_text}."
            )
        except Exception as e:
            await ctx.send(str(e))

    @describe_parameters(channel_id="YouTube channel ID to stop tracking.")
    @youtube.command(name="remove", help="Remove a YouTube channel from tracking.")
    @is_admin()
    async def youtube_remove(self, ctx, channel_id: str):
        if await remove_youtube_subscription(str(ctx.guild.id), channel_id):
            await ctx.send("Removed YouTube channel from tracking.")
        else:
            await ctx.send("YouTube channel not found in subscriptions.")

    @youtube.command(name="list", help="List tracked YouTube channels.")
    @is_admin()
    async def youtube_list(self, ctx):
        subscriptions = await get_youtube_subscriptions_by_guild(str(ctx.guild.id))
        if not subscriptions:
            await ctx.send("No YouTube channels being tracked.")
            return

        embed = discord.Embed(title="YouTube Subscriptions", color=0xFF0000)
        for sub in subscriptions[:10]:
            title = sub.get("channel_title") or sub["youtube_channel_id"]
            embed.add_field(
                name=title,
                value=f"Channel ID: `{sub['youtube_channel_id']}`\nNotifications: <#{sub['notification_channel_id']}>",
                inline=False,
            )
        if len(subscriptions) > 10:
            embed.set_footer(text=f"Showing 10 of {len(subscriptions)} subscriptions")
        await ctx.send(embed=embed)

    @notifications.group(name="twitch", help="Manage Twitch subscriptions.")
    @is_admin()
    async def twitch(self, ctx):
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @describe_parameters(
        username="Twitch username or twitch.tv profile URL to track.",
        channel="Optional text channel for this subscription; otherwise use the default.",
    )
    @twitch.command(
        name="add",
        help="Add a Twitch streamer.\nUsage: !notifications twitch add <username|url> [#target-channel]",
    )
    @is_admin()
    async def twitch_add(
        self, ctx, username: str, channel: Optional[discord.TextChannel] = None
    ):
        if not self.twitch_client_id:
            await ctx.send("Twitch API not configured.")
            return

        target_channel_id = (
            str(channel.id) if channel else await get_notification_channel(str(ctx.guild.id))
        )
        if not target_channel_id:
            await ctx.send(
                "Set a default channel first with `!notifications channel #text-channel`, "
                "or pass one directly: `!notifications twitch add <username> #text-channel`."
            )
            return

        token = await self.get_twitch_token()
        if not token:
            await ctx.send("Failed to authenticate with Twitch API.")
            return

        resolved_username = self._resolve_twitch_username(username)
        if not resolved_username:
            await ctx.send("Could not parse that Twitch user. Provide a username or twitch.tv/<username> URL.")
            return

        async with aiohttp.ClientSession() as session:
            headers = {
                "Client-ID": self.twitch_client_id,
                "Authorization": f"Bearer {token}",
            }
            url = "https://api.twitch.tv/helix/users"
            params = {"login": resolved_username}
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    await ctx.send("Error checking Twitch username.")
                    return
                data = await resp.json()
                if not data.get("data"):
                    await ctx.send("Twitch user not found.")
                    return
                display_name = data["data"][0]["display_name"]

        try:
            await add_twitch_subscription(
                str(ctx.guild.id), resolved_username, target_channel_id, display_name=display_name
            )
            target_text = channel.mention if channel else f"<#{target_channel_id}>"
            await ctx.send(
                f"Added Twitch streamer **{display_name}**. Notifications will post in {target_text}."
            )
        except Exception as e:
            await ctx.send(str(e))

    @describe_parameters(username="Twitch username to stop tracking.")
    @twitch.command(name="remove", help="Remove a Twitch streamer from tracking.")
    @is_admin()
    async def twitch_remove(self, ctx, username: str):
        if await remove_twitch_subscription(str(ctx.guild.id), username.lower()):
            await ctx.send(f"Removed Twitch streamer **{username}** from tracking.")
        else:
            await ctx.send("Twitch streamer not found in subscriptions.")

    @twitch.command(name="list", help="List tracked Twitch streamers.")
    @is_admin()
    async def twitch_list(self, ctx):
        subscriptions = await get_twitch_subscriptions_by_guild(str(ctx.guild.id))
        if not subscriptions:
            await ctx.send("No Twitch streamers being tracked.")
            return

        embed = discord.Embed(title="Twitch Subscriptions", color=0x9146FF)
        for sub in subscriptions[:20]:
            display = sub.get("display_name") or sub["twitch_username"]
            embed.add_field(
                name=display,
                value=(
                    f"Username: `{sub['twitch_username']}`\n"
                    f"Notifications: <#{sub['notification_channel_id']}>"
                ),
                inline=False,
            )
        if len(subscriptions) > 20:
            embed.set_footer(text=f"Showing 20 of {len(subscriptions)} subscriptions")
        await ctx.send(embed=embed)

    def _resolve_twitch_username(self, raw: str) -> Optional[str]:
        """Extract lowercase Twitch username from plain name or twitch.tv URL."""
        ident = raw.strip()
        if ident.startswith("http"):
            try:
                path = ident.split("twitch.tv")[-1]
                parts = [p for p in path.split("/") if p]
                if parts:
                    return parts[0].lower()
            except Exception:
                return None
        return ident.lstrip("@").lower() if ident else None


async def setup(bot):
    await bot.add_cog(Notifications(bot))
