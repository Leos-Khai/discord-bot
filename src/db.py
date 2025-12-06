from __future__ import annotations

from datetime import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

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

    # ---- Lifecycle ----------------------------------------------------- #
    async def initialize(self) -> None:
        await self.channel_links.create_index("voice_channel_id", unique=True)
        await self.custom_messages.create_index(
            [("guild_id", 1), ("type", 1)], unique=True
        )
        await self.music_channel_limits.create_index("guild_id", unique=True)

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


# Singleton-style service to keep current imports stable
_db_service = DatabaseService()


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
