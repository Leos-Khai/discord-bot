import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Sequence

import aiohttp
import discord
from discord.ext import commands, tasks

from cogs.admin import is_admin
from db import (
    add_twitch_subscription,
    add_youtube_subscription,
    get_notification_channel,
    get_stream_status,
    get_twitch_subscriptions,
    get_twitch_subscriptions_by_guild,
    get_youtube_subscriptions,
    get_youtube_subscriptions_by_guild,
    is_video_notified,
    mark_video_notified,
    remove_twitch_subscription,
    remove_youtube_subscription,
    set_notification_channel,
    update_stream_status,
    update_youtube_last_checked,
)
from logger import get_logger


class Notifications(commands.Cog):
    """YouTube and Twitch notifications with per-subscription channels."""

    def __init__(self, bot):
        self.bot = bot
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
            subscriptions = await get_youtube_subscriptions()
            self.logger.info(f"[YouTube] Checking {len(subscriptions)} subscription(s)")

            for sub in subscriptions:
                guild_id = sub["guild_id"]
                youtube_channel_id = sub["youtube_channel_id"]
                channel_title = sub.get("channel_title", youtube_channel_id)
                notification_channel_id = sub["notification_channel_id"]
                raw_last_checked = (
                    sub.get("last_checked")
                    or sub.get("created_at")
                    or datetime.now(timezone.utc)
                )
                last_checked = datetime.now(timezone.utc)
                needs_normalize = False

                if isinstance(raw_last_checked, datetime):
                    last_checked = raw_last_checked
                    if raw_last_checked.tzinfo is None:
                        last_checked = raw_last_checked.replace(tzinfo=timezone.utc)
                        needs_normalize = True
                elif isinstance(raw_last_checked, str):
                    try:
                        parsed = datetime.fromisoformat(raw_last_checked)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        last_checked = parsed
                        needs_normalize = True
                    except ValueError:
                        self.logger.warning(
                            f"[YouTube] Invalid last_checked format for {channel_title}: {raw_last_checked}"
                        )
                        needs_normalize = True
                else:
                    self.logger.warning(
                        f"[YouTube] Unexpected last_checked type for {channel_title}: {type(raw_last_checked)}"
                    )
                    needs_normalize = True

                if needs_normalize:
                    try:
                        await update_youtube_last_checked(
                            guild_id, youtube_channel_id, last_checked
                        )
                    except Exception as norm_err:
                        self.logger.warning(
                            f"[YouTube] Failed to normalize last_checked for {channel_title}: {norm_err}"
                        )

                self.logger.debug(
                    f"[YouTube] Checking {channel_title} (ID: {youtube_channel_id}) for videos since {last_checked.isoformat()}"
                )

                videos = await self._get_youtube_videos(youtube_channel_id, last_checked)

                if not videos:
                    self.logger.debug(f"[YouTube] No new videos for {channel_title}")
                    await update_youtube_last_checked(
                        guild_id, youtube_channel_id, datetime.now(timezone.utc)
                    )
                    continue

                self.logger.info(f"[YouTube] Found {len(videos)} new video(s) for {channel_title}")

                notification_channel = self.bot.get_channel(int(notification_channel_id))
                if not notification_channel:
                    self.logger.error(
                        f"[YouTube] Notification channel {notification_channel_id} not found for {channel_title} (guild: {guild_id})"
                    )
                    continue

                for video in videos:
                    video_id = video["id"]
                    video_title = video["title"]

                    if await is_video_notified(video_id):
                        self.logger.debug(
                            f"[YouTube] Skipping already notified video: {video_title} (ID: {video_id})"
                        )
                        continue

                    try:
                        await self._send_youtube_notification(notification_channel, video)
                        await mark_video_notified(video_id)
                        self.logger.info(
                            f"[YouTube] Sent notification for: {video_title} (ID: {video_id}) from {channel_title}"
                        )
                    except Exception as send_err:
                        self.logger.error(
                            f"[YouTube] Failed to send notification for {video_title}: {send_err}"
                        )

                await update_youtube_last_checked(
                    guild_id, youtube_channel_id, datetime.now(timezone.utc)
                )
        except Exception as e:
            self.logger.error(f"[YouTube] Error in check loop: {e}", exc_info=True)

    @tasks.loop(minutes=2)
    async def check_twitch(self):
        """Poll Twitch subscriptions for live/offline transitions."""
        try:
            subscriptions = await get_twitch_subscriptions()
            if not subscriptions:
                return

            self.logger.info(f"[Twitch] Checking {len(subscriptions)} subscription(s)")

            streamers = list({sub["twitch_username"] for sub in subscriptions})
            for i in range(0, len(streamers), 100):
                batch = streamers[i : i + 100]
                streams = await self._get_twitch_streams(batch)

                if streams is None:
                    self.logger.error(f"[Twitch] Failed to fetch streams for batch: {batch}")
                    continue

                stream_lookup: Dict[str, dict] = {
                    stream["user_login"].lower(): stream for stream in streams
                }

                live_count = len(stream_lookup)
                self.logger.debug(f"[Twitch] Batch {i // 100 + 1}: {live_count} live out of {len(batch)} streamers")

                batch_subscriptions = [
                    sub for sub in subscriptions if sub["twitch_username"] in batch
                ]
                for sub in batch_subscriptions:
                    username = sub["twitch_username"]
                    display_name = sub.get("display_name", username)
                    guild_id = sub["guild_id"]
                    notification_channel_id = sub["notification_channel_id"]

                    current_stream = stream_lookup.get(username.lower())
                    state = await get_stream_status(guild_id, username)
                    was_live = state.get("is_live", False)
                    notification_channel = self.bot.get_channel(
                        int(notification_channel_id)
                    )

                    if not notification_channel:
                        self.logger.error(
                            f"[Twitch] Notification channel {notification_channel_id} not found for {display_name} (guild: {guild_id})"
                        )

                    if current_stream and not was_live:
                        # Streamer went live
                        self.logger.info(
                            f"[Twitch] {display_name} went LIVE - Game: {current_stream.get('game_name', 'Unknown')}, "
                            f"Viewers: {current_stream.get('viewer_count', 0)}"
                        )
                        message_id = None
                        if notification_channel:
                            try:
                                message_id = await self._send_twitch_notification(
                                    notification_channel, current_stream, "live"
                                )
                                self.logger.info(f"[Twitch] Sent live notification for {display_name}")
                            except Exception as send_err:
                                self.logger.error(
                                    f"[Twitch] Failed to send live notification for {display_name}: {send_err}"
                                )
                        await update_stream_status(
                            guild_id,
                            username,
                            True,
                            current_stream["id"],
                            message_id=message_id,
                            user_id=current_stream.get("user_id"),
                            user_login=current_stream.get("user_login"),
                            display_name=current_stream.get("user_name"),
                        )
                    elif not current_stream and was_live:
                        # Streamer went offline
                        self.logger.info(f"[Twitch] {display_name} went OFFLINE")
                        offline_payload = {
                            "user_name": state.get("display_name")
                            or sub.get("display_name")
                            or username,
                            "display_name": state.get("display_name")
                            or sub.get("display_name")
                            or username,
                            "user_login": state.get("user_login") or username,
                            "user_id": state.get("user_id"),
                            "stream_id": state.get("stream_id"),
                            "message_id": state.get("message_id"),
                        }
                        message_id = offline_payload["message_id"]
                        if notification_channel:
                            try:
                                message_id = await self._send_twitch_notification(
                                    notification_channel, offline_payload, "offline"
                                )
                                self.logger.info(f"[Twitch] Sent/edited offline notification for {display_name}")
                            except Exception as send_err:
                                self.logger.error(
                                    f"[Twitch] Failed to send offline notification for {display_name}: {send_err}"
                                )
                        await update_stream_status(
                            guild_id,
                            username,
                            False,
                            None,
                            message_id=message_id,
                            user_id=offline_payload["user_id"],
                            user_login=offline_payload["user_login"],
                            display_name=offline_payload["display_name"],
                        )
        except Exception as e:
            self.logger.error(f"[Twitch] Error in check loop: {e}", exc_info=True)

    # --- External API helpers --------------------------------------------
    async def _get_youtube_videos(self, channel_id: str, since: datetime):
        """Fetch recent videos from a YouTube channel."""
        if not self.youtube_api_key:
            self.logger.warning("[YouTube API] No API key configured")
            return []

        if isinstance(since, str):
            try:
                since = datetime.fromisoformat(since)
            except ValueError:
                self.logger.warning(f"[YouTube API] Invalid since datetime string: {since}")
                since = datetime.now(timezone.utc)
        if not isinstance(since, datetime):
            since = datetime.now(timezone.utc)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

        async with aiohttp.ClientSession() as session:
            # Step 1: Get the uploads playlist ID
            url = "https://www.googleapis.com/youtube/v3/channels"
            params = {
                "key": self.youtube_api_key,
                "id": channel_id,
                "part": "contentDetails",
            }
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    response_text = await resp.text()
                    self.logger.error(
                        f"[YouTube API] Failed to get channel {channel_id}: "
                        f"HTTP {resp.status} - {response_text[:500]}"
                    )
                    return []
                data = await resp.json()

                # Check for API errors in response
                if "error" in data:
                    error_info = data["error"]
                    self.logger.error(
                        f"[YouTube API] API error for channel {channel_id}: "
                        f"Code {error_info.get('code')} - {error_info.get('message')}"
                    )
                    return []

                if not data.get("items"):
                    self.logger.warning(
                        f"[YouTube API] No channel found for ID: {channel_id}"
                    )
                    return []

                uploads_playlist = data["items"][0]["contentDetails"]["relatedPlaylists"][
                    "uploads"
                ]
                self.logger.debug(
                    f"[YouTube API] Channel {channel_id} uploads playlist: {uploads_playlist}"
                )

            # Step 2: Get recent videos from uploads playlist
            url = "https://www.googleapis.com/youtube/v3/playlistItems"
            params = {
                "key": self.youtube_api_key,
                "playlistId": uploads_playlist,
                "part": "snippet",
                "maxResults": 10,
                "order": "date",
            }
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    response_text = await resp.text()
                    self.logger.error(
                        f"[YouTube API] Failed to get playlist {uploads_playlist}: "
                        f"HTTP {resp.status} - {response_text[:500]}"
                    )
                    return []
                data = await resp.json()

                # Check for API errors in response
                if "error" in data:
                    error_info = data["error"]
                    self.logger.error(
                        f"[YouTube API] API error for playlist {uploads_playlist}: "
                        f"Code {error_info.get('code')} - {error_info.get('message')}"
                    )
                    return []

        items = data.get("items", [])
        self.logger.debug(
            f"[YouTube API] Retrieved {len(items)} items from playlist for channel {channel_id}"
        )

        videos = []
        skipped_older = 0
        for item in items:
            try:
                published = datetime.fromisoformat(
                    item["snippet"]["publishedAt"].replace("Z", "+00:00")
                )
                video_id = item["snippet"]["resourceId"]["videoId"]
                video_title = item["snippet"]["title"]

                if published > since:
                    videos.append(
                        {
                            "id": video_id,
                            "title": video_title,
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "thumbnail": item["snippet"]["thumbnails"].get("medium", {}).get("url", ""),
                            "channel_name": item["snippet"]["channelTitle"],
                            "published_at": published,
                        }
                    )
                    self.logger.debug(
                        f"[YouTube API] New video found: {video_title} (published: {published.isoformat()})"
                    )
                else:
                    skipped_older += 1
                    self.logger.debug(
                        f"[YouTube API] Skipping older video: {video_title} "
                        f"(published: {published.isoformat()}, since: {since.isoformat()})"
                    )
            except Exception as parse_err:
                self.logger.error(
                    f"[YouTube API] Error parsing video item: {parse_err} - Item: {item}"
                )

        self.logger.debug(
            f"[YouTube API] Channel {channel_id}: {len(videos)} new, {skipped_older} older than {since.isoformat()}"
        )
        return videos

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

    async def _send_youtube_notification(self, channel: discord.TextChannel, video: dict):
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
        self, channel: discord.TextChannel, stream_data: dict, status: str
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
        vod_url = await self._get_twitch_vod_url(
            stream_data.get("user_id"), stream_data.get("stream_id"), user_login
        )
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
