from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError
from twitch_transitions import (
    DeliveryStatus,
    TwitchDeliveryState,
    TwitchSubscription,
)
from youtube_notification_delivery import (
    PublicationKind,
    YouTubeDeliveryState,
    YouTubeDeliveryStatus,
    YouTubePublication,
    YouTubeSubscription,
)

script_dir = os.path.dirname(os.path.abspath(__file__))


class DatabaseError(Exception):
    """Base class for database errors."""


class DuplicateChannelLinkError(DatabaseError):
    """Raised when attempting to link a voice channel that already exists."""


class DatabaseConfig:
    """Lightweight loader for Mongo connection settings."""

    def __init__(self, uri: str, database: str):
        self.uri = uri
        self.database = database

    @classmethod
    def load(cls) -> "DatabaseConfig":
        # Prefer env vars; fallback to config.json for local dev
        env_uri = os.getenv("MONGODB_URI")
        env_db = os.getenv("MONGODB_DATABASE")
        if env_uri and env_db:
            return cls(env_uri, env_db)

        with open(os.path.join(script_dir, "config.json")) as f:
            config = json.load(f)

        mongodb = config.get(
            "mongodb", {"uri": "mongodb://localhost:27017", "database": "discord_bot"}
        )
        return cls(mongodb["uri"], mongodb["database"])


class DatabaseService:
    """Async MongoDB service with domain-specific helpers."""

    def __init__(self, config: Optional[DatabaseConfig] = None):
        cfg = config or DatabaseConfig.load()
        self.client = AsyncIOMotorClient(cfg.uri)
        self.db = self.client[cfg.database]

        # Collections
        self.servers = self.db.servers
        self.channel_links = self.db.channel_links
        self.custom_messages = self.db.custom_messages
        self.music_channel_limits = self.db.music_channel_limits
        self.guild_playback_settings = self.db.guild_playback_settings
        self.user_tts_voices = self.db.user_tts_voices
        self.notification_channels = self.db.notification_channels
        self.youtube_subscriptions = self.db.youtube_subscriptions
        self.youtube_channel_observations = self.db.youtube_channel_observations
        self.youtube_delivery_states = self.db.youtube_delivery_states
        self.twitch_subscriptions = self.db.twitch_subscriptions
        self.twitch_stream_status = self.db.twitch_stream_status
        self.youtube_channel_meta = self.db.youtube_channel_meta
        self.twitch_user_meta = self.db.twitch_user_meta

    # ---- Lifecycle ----------------------------------------------------- #
    async def initialize(self) -> None:
        await self.channel_links.create_index("voice_channel_id", unique=True)
        await self.custom_messages.create_index(
            [("guild_id", 1), ("type", 1)], unique=True
        )
        await self.music_channel_limits.create_index("guild_id", unique=True)
        await self.guild_playback_settings.create_index("guild_id", unique=True)
        await self.user_tts_voices.create_index([("guild_id", 1), ("user_id", 1)], unique=True)
        await self.notification_channels.create_index("guild_id", unique=True)
        await self.youtube_subscriptions.create_index(
            [("guild_id", 1), ("youtube_channel_id", 1)], unique=True
        )
        await self.youtube_channel_observations.create_index(
            "youtube_channel_id", unique=True
        )
        await self.youtube_delivery_states.create_index(
            [("guild_id", 1), ("youtube_channel_id", 1), ("publication_id", 1)],
            unique=True,
        )
        await self.twitch_subscriptions.create_index(
            [("guild_id", 1), ("twitch_username", 1)], unique=True
        )
        await self.twitch_stream_status.create_index(
            [("guild_id", 1), ("twitch_username", 1)], unique=True
        )
        await self.youtube_channel_meta.create_index("channel_id", unique=True)
        await self.twitch_user_meta.create_index("username", unique=True)

        baseline = datetime.now(timezone.utc).isoformat()
        for channel_id in await self.youtube_subscriptions.distinct(
            "youtube_channel_id"
        ):
            await self.youtube_channel_observations.update_one(
                {"youtube_channel_id": channel_id},
                {
                    "$setOnInsert": {
                        "youtube_channel_id": channel_id,
                        "cursor": baseline,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
                upsert=True,
            )

    # ---- Servers ------------------------------------------------------- #
    async def add_server(self, server_id: str) -> None:
        await self.servers.update_one(
            {"server_id": server_id},
            {"$setOnInsert": {"server_id": server_id, "created_at": datetime.utcnow()}},
            upsert=True,
        )

    async def get_servers(self) -> List[Dict[str, Any]]:
        cursor = self.servers.find({}, {"_id": 0})
        return await cursor.to_list(None)

    # ---- Channel Links ------------------------------------------------- #
    async def add_channel_link(
        self,
        guild_id: str,
        text_channel_id: str,
        voice_channel_id: str,
        role_id: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow()
        try:
            await self.channel_links.insert_one(
                {
                    "guild_id": guild_id,
                    "text_channel_id": text_channel_id,
                    "voice_channel_id": voice_channel_id,
                    "role_id": role_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        except DuplicateKeyError as e:
            raise DuplicateChannelLinkError(
                "The specified voice channel is already linked."
            ) from e

    async def get_channel_link(
        self, voice_channel_id: str
    ) -> Optional[Tuple[str, str, Optional[str]]]:
        result = await self.channel_links.find_one(
            {"voice_channel_id": voice_channel_id},
            {"_id": 0, "created_at": 0, "updated_at": 0},
        )
        if result:
            return (
                result["guild_id"],
                result["text_channel_id"],
                result.get("role_id"),
            )
        return None

    async def remove_channel_link(self, link_id) -> None:
        await self.channel_links.delete_one({"_id": link_id})

    async def update_channel_link_text(
        self, voice_channel_id: str, new_text_channel_id: str
    ) -> bool:
        result = await self.channel_links.update_one(
            {"voice_channel_id": voice_channel_id},
            {
                "$set": {
                    "text_channel_id": new_text_channel_id,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        return result.modified_count > 0

    async def update_channel_link_role(
        self, voice_channel_id: str, new_role_id: Optional[str]
    ) -> bool:
        result = await self.channel_links.update_one(
            {"voice_channel_id": voice_channel_id},
            {"$set": {"role_id": new_role_id, "updated_at": datetime.utcnow()}},
        )
        return result.modified_count > 0

    async def get_channel_links_by_guild(
        self, guild_id: str
    ) -> List[Tuple[Any, str, str, Optional[str]]]:
        cursor = self.channel_links.find(
            {"guild_id": guild_id}, {"created_at": 0, "updated_at": 0}
        )
        links = await cursor.to_list(None)
        return [
            (
                link["_id"],
                link["text_channel_id"],
                link["voice_channel_id"],
                link.get("role_id"),
            )
            for link in links
        ]

    # ---- Custom Messages ----------------------------------------------- #
    async def set_custom_message(
        self, guild_id: str, msg_type: str, message: Optional[str]
    ) -> None:
        if msg_type not in ["join", "leave", "move"]:
            raise ValueError("Message type must be 'join', 'leave', or 'move'")

        now = datetime.utcnow()
        if message is None:
            await self.custom_messages.delete_one(
                {"guild_id": guild_id, "type": msg_type}
            )
        else:
            await self.custom_messages.update_one(
                {"guild_id": guild_id, "type": msg_type},
                {
                    "$set": {"message": message, "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                },
                upsert=True,
            )

    async def get_custom_message(self, guild_id: str, msg_type: str) -> Optional[str]:
        result = await self.custom_messages.find_one(
            {"guild_id": guild_id, "type": msg_type},
            {"_id": 0, "created_at": 0, "updated_at": 0},
        )
        return result["message"] if result else None

    # ---- Music Channel Limits ------------------------------------------ #
    async def get_music_channels(self, guild_id: str) -> List[str]:
        doc = await self.music_channel_limits.find_one(
            {"guild_id": guild_id}, {"_id": 0, "channel_ids": 1}
        )
        return doc.get("channel_ids", []) if doc else []

    async def set_music_channels(
        self, guild_id: str, channel_ids: List[str]
    ) -> List[str]:
        now = datetime.utcnow()
        unique_channels = list({str(cid) for cid in channel_ids})
        await self.music_channel_limits.update_one(
            {"guild_id": guild_id},
            {
                "$set": {"channel_ids": unique_channels, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return unique_channels

    async def add_music_channel(self, guild_id: str, channel_id: str) -> List[str]:
        now = datetime.utcnow()
        await self.music_channel_limits.update_one(
            {"guild_id": guild_id},
            {
                "$addToSet": {"channel_ids": str(channel_id)},
                "$set": {"updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return await self.get_music_channels(guild_id)

    async def remove_music_channel(self, guild_id: str, channel_id: str) -> List[str]:
        await self.music_channel_limits.update_one(
            {"guild_id": guild_id}, {"$pull": {"channel_ids": str(channel_id)}}
        )
        remaining = await self.get_music_channels(guild_id)
        if not remaining:
            await self.music_channel_limits.delete_one({"guild_id": guild_id})
        return remaining

    async def clear_music_channels(self, guild_id: str) -> List[str]:
        await self.music_channel_limits.delete_one({"guild_id": guild_id})
        return []

    # ---- Guild Playback ------------------------------------------------ #
    async def get_playback_volume(self, guild_id: str) -> float:
        doc = await self.guild_playback_settings.find_one(
            {"guild_id": guild_id}, {"_id": 0, "volume": 1}
        )
        return float(doc.get("volume", 1.0)) if doc else 1.0

    async def set_playback_volume(self, guild_id: str, volume: float) -> None:
        now = datetime.utcnow()
        await self.guild_playback_settings.update_one(
            {"guild_id": guild_id},
            {
                "$set": {"volume": volume, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    # ---- TTS ----------------------------------------------------------- #
    async def get_tts_voice(self, guild_id: str, user_id: str) -> Optional[str]:
        doc = await self.user_tts_voices.find_one(
            {"guild_id": guild_id, "user_id": user_id}, {"_id": 0, "voice": 1}
        )
        return doc.get("voice") if doc else None

    async def set_tts_voice(self, guild_id: str, user_id: str, voice: str) -> None:
        now = datetime.utcnow()
        await self.user_tts_voices.update_one(
            {"guild_id": guild_id, "user_id": user_id},
            {
                "$set": {"voice": voice, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    # ---- Notifications (YouTube + Twitch) ------------------------------ #
    async def set_notification_channel(self, guild_id: str, channel_id: str) -> None:
        now = datetime.utcnow()
        await self.notification_channels.update_one(
            {"guild_id": guild_id},
            {
                "$set": {"channel_id": channel_id, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def get_notification_channel(self, guild_id: str) -> Optional[str]:
        doc = await self.notification_channels.find_one(
            {"guild_id": guild_id}, {"_id": 0, "channel_id": 1}
        )
        return doc["channel_id"] if doc else None

    async def add_youtube_subscription(
        self,
        guild_id: str,
        youtube_channel_id: str,
        notification_channel_id: str,
        channel_title: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        try:
            await self.youtube_subscriptions.insert_one(
                {
                    "guild_id": guild_id,
                    "youtube_channel_id": youtube_channel_id,
                    "notification_channel_id": notification_channel_id,
                    "channel_title": channel_title,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            await self.youtube_channel_observations.update_one(
                {"youtube_channel_id": youtube_channel_id},
                {
                    "$setOnInsert": {
                        "youtube_channel_id": youtube_channel_id,
                        "cursor": now.isoformat(),
                        "updated_at": now,
                    }
                },
                upsert=True,
            )
        except DuplicateKeyError as e:
            raise DatabaseError("YouTube channel already tracked for this guild.") from e

    async def remove_youtube_subscription(self, guild_id: str, youtube_channel_id: str) -> bool:
        result = await self.youtube_subscriptions.delete_one(
            {"guild_id": guild_id, "youtube_channel_id": youtube_channel_id}
        )
        if result.deleted_count:
            await self.youtube_delivery_states.delete_many(
                {"guild_id": guild_id, "youtube_channel_id": youtube_channel_id}
            )
            remaining = await self.youtube_subscriptions.count_documents(
                {"youtube_channel_id": youtube_channel_id}, limit=1
            )
            if not remaining:
                await self.youtube_channel_observations.delete_one(
                    {"youtube_channel_id": youtube_channel_id}
                )
        return result.deleted_count > 0

    async def get_youtube_subscriptions(self) -> List[Dict[str, Any]]:
        cursor = self.youtube_subscriptions.find({})
        return await cursor.to_list(None)

    async def get_youtube_subscriptions_by_guild(
        self, guild_id: str
    ) -> List[Dict[str, Any]]:
        cursor = self.youtube_subscriptions.find({"guild_id": guild_id})
        return await cursor.to_list(None)

    async def tracked_youtube_subscriptions(self) -> List[YouTubeSubscription]:
        subscriptions = await self.get_youtube_subscriptions()
        return [
            YouTubeSubscription(
                guild_id=str(subscription["guild_id"]),
                youtube_channel_id=str(subscription["youtube_channel_id"]),
                notification_channel_id=str(subscription["notification_channel_id"]),
                subscribed_at=self._aware_datetime(
                    subscription.get("created_at") or datetime.now(timezone.utc)
                ),
                channel_title=subscription.get("channel_title"),
            )
            for subscription in subscriptions
        ]

    async def youtube_observation_cursor(self, channel_id: str) -> Optional[str]:
        observation = await self.youtube_channel_observations.find_one(
            {"youtube_channel_id": channel_id}, {"cursor": 1}
        )
        return observation.get("cursor") if observation else None

    async def record_youtube_observation(
        self,
        channel_id: str,
        cursor: str,
        deliveries: Sequence[YouTubeDeliveryState],
    ) -> None:
        for delivery in deliveries:
            document = self._youtube_delivery_document(delivery)
            await self.youtube_delivery_states.update_one(
                self._youtube_delivery_filter(delivery),
                {"$setOnInsert": document},
                upsert=True,
            )
        await self.youtube_channel_observations.update_one(
            {"youtube_channel_id": channel_id},
            {
                "$set": {
                    "cursor": cursor,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    async def pending_youtube_deliveries(self) -> List[YouTubeDeliveryState]:
        cursor = self.youtube_delivery_states.find(
            {"delivery_status": YouTubeDeliveryStatus.PENDING.value}
        )
        return [
            self._youtube_delivery_state(document)
            for document in await cursor.to_list(None)
        ]

    async def save_youtube_delivery(self, delivery: YouTubeDeliveryState) -> None:
        result = await self.youtube_delivery_states.update_one(
            self._youtube_delivery_filter(delivery),
            {
                "$set": {
                    "delivery_status": delivery.delivery_status.value,
                    "delivery_attempts": delivery.delivery_attempts,
                    "message_id": delivery.message_id,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        if result.matched_count != 1:
            raise DatabaseError(
                "YouTube delivery no longer exists; dispatch was not persisted."
            )

    @staticmethod
    def _youtube_delivery_filter(delivery: YouTubeDeliveryState) -> dict:
        return {
            "guild_id": delivery.guild_id,
            "youtube_channel_id": delivery.youtube_channel_id,
            "publication_id": delivery.publication.publication_id,
        }

    @staticmethod
    def _aware_datetime(value) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @classmethod
    def _youtube_delivery_document(cls, delivery: YouTubeDeliveryState) -> dict:
        publication = delivery.publication
        now = datetime.now(timezone.utc)
        return {
            "guild_id": delivery.guild_id,
            "youtube_channel_id": delivery.youtube_channel_id,
            "publication_id": publication.publication_id,
            "publication": {
                "channel_id": publication.channel_id,
                "title": publication.title,
                "url": publication.url,
                "thumbnail_url": publication.thumbnail_url,
                "channel_name": publication.channel_name,
                "published_at": publication.published_at,
                "kind": publication.kind.value,
            },
            "delivery_status": delivery.delivery_status.value,
            "delivery_attempts": delivery.delivery_attempts,
            "message_id": delivery.message_id,
            "created_at": now,
            "updated_at": now,
        }

    @classmethod
    def _youtube_delivery_state(cls, document: dict) -> YouTubeDeliveryState:
        publication = document["publication"]
        try:
            kind = PublicationKind(publication.get("kind", PublicationKind.VIDEO.value))
        except ValueError:
            kind = PublicationKind.VIDEO
        return YouTubeDeliveryState(
            guild_id=str(document["guild_id"]),
            youtube_channel_id=str(document["youtube_channel_id"]),
            publication=YouTubePublication(
                publication_id=str(document["publication_id"]),
                channel_id=str(publication["channel_id"]),
                title=publication["title"],
                url=publication["url"],
                thumbnail_url=publication.get("thumbnail_url", ""),
                channel_name=publication["channel_name"],
                published_at=cls._aware_datetime(publication["published_at"]),
                kind=kind,
            ),
            delivery_status=YouTubeDeliveryStatus(document["delivery_status"]),
            delivery_attempts=int(document.get("delivery_attempts", 0)),
            message_id=document.get("message_id"),
        )

    async def add_twitch_subscription(
        self,
        guild_id: str,
        twitch_username: str,
        notification_channel_id: str,
        display_name: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow()
        try:
            await self.twitch_subscriptions.insert_one(
                {
                    "guild_id": guild_id,
                    "twitch_username": twitch_username.lower(),
                    "notification_channel_id": notification_channel_id,
                    "display_name": display_name,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        except DuplicateKeyError as e:
            raise DatabaseError("Twitch user already tracked for this guild.") from e

    async def remove_twitch_subscription(self, guild_id: str, twitch_username: str) -> bool:
        result = await self.twitch_subscriptions.delete_one(
            {"guild_id": guild_id, "twitch_username": twitch_username.lower()}
        )
        if result.deleted_count:
            await self.twitch_stream_status.delete_one(
                {"guild_id": guild_id, "twitch_username": twitch_username.lower()}
            )
        return result.deleted_count > 0

    async def get_twitch_subscriptions(self) -> List[Dict[str, Any]]:
        cursor = self.twitch_subscriptions.find({})
        return await cursor.to_list(None)

    async def get_twitch_subscriptions_by_guild(
        self, guild_id: str
    ) -> List[Dict[str, Any]]:
        cursor = self.twitch_subscriptions.find({"guild_id": guild_id})
        return await cursor.to_list(None)

    async def tracked_subscriptions(self) -> List[TwitchSubscription]:
        subscriptions = await self.get_twitch_subscriptions()
        return [
            TwitchSubscription(
                guild_id=subscription["guild_id"],
                twitch_username=subscription["twitch_username"],
                notification_channel_id=subscription["notification_channel_id"],
                display_name=subscription.get("display_name"),
            )
            for subscription in subscriptions
        ]

    async def delivery_state(
        self, guild_id: str, twitch_username: str
    ) -> Optional[TwitchDeliveryState]:
        doc = await self.twitch_stream_status.find_one(
            {"guild_id": guild_id, "twitch_username": twitch_username.lower()},
            {"_id": 0},
        )
        if not doc:
            return None
        try:
            delivery_status = DeliveryStatus(
                doc.get("delivery_status", DeliveryStatus.DELIVERED)
            )
        except ValueError:
            delivery_status = DeliveryStatus.DELIVERED
        return TwitchDeliveryState(
            guild_id=doc["guild_id"],
            twitch_username=doc["twitch_username"],
            is_live=doc.get("is_live", False),
            stream_id=doc.get("stream_id"),
            message_id=doc.get("message_id"),
            delivery_status=delivery_status,
            delivery_attempts=doc.get("delivery_attempts", 0),
            user_id=doc.get("user_id"),
            user_login=doc.get("user_login"),
            display_name=doc.get("display_name"),
        )

    async def save_delivery_state(self, state: TwitchDeliveryState) -> None:
        now = datetime.utcnow()
        await self.twitch_stream_status.update_one(
            {
                "guild_id": state.guild_id,
                "twitch_username": state.twitch_username.lower(),
            },
            {
                "$set": {
                    "is_live": state.is_live,
                    "stream_id": state.stream_id,
                    "message_id": state.message_id,
                    "delivery_status": state.delivery_status.value,
                    "delivery_attempts": state.delivery_attempts,
                    "user_id": state.user_id,
                    "user_login": state.user_login.lower() if state.user_login else None,
                    "display_name": state.display_name,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    async def get_stream_status(
        self, guild_id: str, twitch_username: str
    ) -> Dict[str, Any]:
        doc = await self.twitch_stream_status.find_one(
            {"guild_id": guild_id, "twitch_username": twitch_username.lower()},
            {"_id": 0},
        )
        if not doc:
            return {
                "is_live": False,
                "stream_id": None,
                "message_id": None,
                "user_id": None,
                "user_login": twitch_username.lower(),
                "display_name": None,
            }
        doc.setdefault("is_live", False)
        doc.setdefault("stream_id", None)
        doc.setdefault("message_id", None)
        doc.setdefault("user_id", None)
        doc.setdefault("user_login", twitch_username.lower())
        doc.setdefault("display_name", None)
        return doc

    async def update_stream_status(
        self,
        guild_id: str,
        twitch_username: str,
        is_live: bool,
        stream_id: Optional[str],
        message_id: Optional[str] = None,
        user_id: Optional[str] = None,
        user_login: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        now = datetime.utcnow()
        set_fields = {
            "is_live": is_live,
            "stream_id": stream_id,
            "message_id": message_id,
            "user_id": user_id,
            "user_login": user_login.lower() if user_login else None,
            "display_name": display_name,
            "updated_at": now,
        }
        await self.twitch_stream_status.update_one(
            {"guild_id": guild_id, "twitch_username": twitch_username.lower()},
            {
                "$set": set_fields,
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    # ---- Metadata caches (optional) ------------------------------------ #
    async def upsert_youtube_meta(self, channel_id: str, title: str):
        await self.youtube_channel_meta.update_one(
            {"channel_id": channel_id},
            {"$set": {"title": title, "updated_at": datetime.utcnow()}},
            upsert=True,
        )

    async def get_youtube_meta(self, channel_id: str) -> Optional[str]:
        doc = await self.youtube_channel_meta.find_one(
            {"channel_id": channel_id}, {"title": 1, "_id": 0}
        )
        return doc["title"] if doc else None

    async def upsert_twitch_meta(self, username: str, display_name: str):
        await self.twitch_user_meta.update_one(
            {"username": username.lower()},
            {"$set": {"display_name": display_name, "updated_at": datetime.utcnow()}},
            upsert=True,
        )

    async def get_twitch_meta(self, username: str) -> Optional[str]:
        doc = await self.twitch_user_meta.find_one(
            {"username": username.lower()}, {"display_name": 1, "_id": 0}
        )
        return doc["display_name"] if doc else None


# Singleton-style service to keep current imports stable
_db_service = DatabaseService()


def get_database_service() -> DatabaseService:
    return _db_service


async def initialize_database():
    await _db_service.initialize()


# Compatibility wrappers; callers can migrate to _db_service.* as needed
async def add_server(server_id: str):
    return await _db_service.add_server(server_id)


async def get_servers():
    return await _db_service.get_servers()


async def add_channel_link(guild_id, text_channel_id, voice_channel_id, role_id=None):
    try:
        return await _db_service.add_channel_link(
            guild_id, text_channel_id, voice_channel_id, role_id
        )
    except DuplicateChannelLinkError as e:
        raise ValueError(str(e)) from e


async def get_channel_link(voice_channel_id):
    return await _db_service.get_channel_link(voice_channel_id)


async def remove_channel_link(link_id):
    return await _db_service.remove_channel_link(link_id)


async def update_channel_link_text(voice_channel_id, new_text_channel_id):
    return await _db_service.update_channel_link_text(
        voice_channel_id, new_text_channel_id
    )


async def update_channel_link_role(voice_channel_id, new_role_id):
    return await _db_service.update_channel_link_role(voice_channel_id, new_role_id)


async def get_channel_links_by_guild(guild_id):
    return await _db_service.get_channel_links_by_guild(guild_id)


async def set_custom_message(guild_id, msg_type, message):
    return await _db_service.set_custom_message(guild_id, msg_type, message)


async def get_custom_message(guild_id, msg_type):
    return await _db_service.get_custom_message(guild_id, msg_type)


async def get_music_channels(guild_id):
    return await _db_service.get_music_channels(guild_id)


async def set_music_channels(guild_id, channel_ids):
    return await _db_service.set_music_channels(guild_id, channel_ids)


async def add_music_channel(guild_id, channel_id):
    return await _db_service.add_music_channel(guild_id, channel_id)


async def remove_music_channel(guild_id, channel_id):
    return await _db_service.remove_music_channel(guild_id, channel_id)


async def clear_music_channels(guild_id):
    return await _db_service.clear_music_channels(guild_id)


async def get_playback_volume(guild_id: str):
    return await _db_service.get_playback_volume(guild_id)


async def set_playback_volume(guild_id: str, volume: float):
    return await _db_service.set_playback_volume(guild_id, volume)


async def get_tts_voice(guild_id: str, user_id: str):
    return await _db_service.get_tts_voice(guild_id, user_id)


async def set_tts_voice(guild_id: str, user_id: str, voice: str):
    return await _db_service.set_tts_voice(guild_id, user_id, voice)


# Notifications
async def set_notification_channel(guild_id: str, channel_id: str):
    return await _db_service.set_notification_channel(guild_id, channel_id)


async def get_notification_channel(guild_id: str):
    return await _db_service.get_notification_channel(guild_id)


async def add_youtube_subscription(
    guild_id: str,
    youtube_channel_id: str,
    notification_channel_id: str,
    channel_title: Optional[str] = None,
):
    return await _db_service.add_youtube_subscription(
        guild_id, youtube_channel_id, notification_channel_id, channel_title
    )


async def remove_youtube_subscription(guild_id: str, youtube_channel_id: str):
    return await _db_service.remove_youtube_subscription(guild_id, youtube_channel_id)


async def get_youtube_subscriptions():
    return await _db_service.get_youtube_subscriptions()


async def get_youtube_subscriptions_by_guild(guild_id: str):
    return await _db_service.get_youtube_subscriptions_by_guild(guild_id)


async def add_twitch_subscription(
    guild_id: str,
    twitch_username: str,
    notification_channel_id: str,
    display_name: Optional[str] = None,
):
    return await _db_service.add_twitch_subscription(
        guild_id, twitch_username, notification_channel_id, display_name
    )


async def upsert_youtube_meta(channel_id: str, title: str):
    return await _db_service.upsert_youtube_meta(channel_id, title)


async def get_youtube_meta(channel_id: str):
    return await _db_service.get_youtube_meta(channel_id)


async def upsert_twitch_meta(username: str, display_name: str):
    return await _db_service.upsert_twitch_meta(username, display_name)


async def get_twitch_meta(username: str):
    return await _db_service.get_twitch_meta(username)


async def remove_twitch_subscription(guild_id: str, twitch_username: str):
    return await _db_service.remove_twitch_subscription(guild_id, twitch_username)


async def get_twitch_subscriptions():
    return await _db_service.get_twitch_subscriptions()


async def get_twitch_subscriptions_by_guild(guild_id: str):
    return await _db_service.get_twitch_subscriptions_by_guild(guild_id)


async def get_stream_status(guild_id: str, twitch_username: str):
    return await _db_service.get_stream_status(guild_id, twitch_username)


async def update_stream_status(
    guild_id: str,
    twitch_username: str,
    is_live: bool,
    stream_id=None,
    message_id=None,
    user_id=None,
    user_login=None,
    display_name=None,
):
    return await _db_service.update_stream_status(
        guild_id,
        twitch_username,
        is_live,
        stream_id,
        message_id=message_id,
        user_id=user_id,
        user_login=user_login,
        display_name=display_name,
    )
