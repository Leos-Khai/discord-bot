from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
import json
import os

script_dir = os.path.dirname(os.path.abspath(__file__))

# Load MongoDB configuration
with open(os.path.join(script_dir, "config.json")) as f:
    config = json.load(f)

mongodb_config = config.get(
    "mongodb", {"uri": "mongodb://localhost:27017", "database": "discord_bot"}
)


class Database:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.client = AsyncIOMotorClient(mongodb_config["uri"])
        self.db = self.client[mongodb_config["database"]]

        # Collections
        self.servers = self.db.servers
        self.channel_links = self.db.channel_links
        self.custom_messages = self.db.custom_messages


async def initialize_database():
    """Initialize MongoDB and create indexes."""
    db = Database.get_instance()

    # Create indexes
    await db.channel_links.create_index("voice_channel_id", unique=True)
    await db.custom_messages.create_index([("guild_id", 1), ("type", 1)], unique=True)


async def add_server(server_id: str):
    """Add a server ID to the database."""
    db = Database.get_instance()
    await db.servers.update_one(
        {"server_id": server_id},
        {"$setOnInsert": {"server_id": server_id, "created_at": datetime.utcnow()}},
        upsert=True,
    )


async def get_servers():
    """Fetch all server IDs from the database."""
    db = Database.get_instance()
    cursor = db.servers.find({}, {"_id": 0})
    return await cursor.to_list(None)


async def add_channel_link(guild_id, text_channel_id, voice_channel_id, role_id=None):
    """Add a channel link to the database."""
    db = Database.get_instance()
    now = datetime.utcnow()
    try:
        await db.channel_links.insert_one(
            {
                "guild_id": guild_id,
                "text_channel_id": text_channel_id,
                "voice_channel_id": voice_channel_id,
                "role_id": role_id,
                "created_at": now,
                "updated_at": now,
            }
        )
    except Exception as e:
        raise ValueError("The specified voice channel is already linked.")


async def get_channel_link(voice_channel_id):
    """Retrieve a channel link by voice channel ID."""
    db = Database.get_instance()
    result = await db.channel_links.find_one(
        {"voice_channel_id": voice_channel_id},
        {"_id": 0, "created_at": 0, "updated_at": 0},
    )
    if result:
        return result["guild_id"], result["text_channel_id"], result.get("role_id")
    return None


async def remove_channel_link(link_id):
    """Remove a channel link by its ID."""
    db = Database.get_instance()
    await db.channel_links.delete_one({"_id": link_id})


async def update_channel_link_text(voice_channel_id, new_text_channel_id):
    """Update the text channel ID for a voice channel link."""
    db = Database.get_instance()
    result = await db.channel_links.update_one(
        {"voice_channel_id": voice_channel_id},
        {
            "$set": {
                "text_channel_id": new_text_channel_id,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return result.modified_count > 0


async def update_channel_link_role(voice_channel_id, new_role_id):
    """Update the role ID for a voice channel link."""
    db = Database.get_instance()
    result = await db.channel_links.update_one(
        {"voice_channel_id": voice_channel_id},
        {"$set": {"role_id": new_role_id, "updated_at": datetime.utcnow()}},
    )
    return result.modified_count > 0


async def get_channel_links_by_guild(guild_id):
    """Retrieve all channel links for a specific guild."""
    db = Database.get_instance()
    cursor = db.channel_links.find(
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


async def set_custom_message(guild_id, msg_type, message):
    """Set a custom message for a specific guild and type (join/leave/move)."""
    if msg_type not in ["join", "leave", "move"]:
        raise ValueError("Message type must be 'join', 'leave', or 'move'")

    db = Database.get_instance()
    now = datetime.utcnow()

    if message is None:
        # Remove the custom message to revert to default
        await db.custom_messages.delete_one({"guild_id": guild_id, "type": msg_type})
    else:
        await db.custom_messages.update_one(
            {"guild_id": guild_id, "type": msg_type},
            {
                "$set": {"message": message, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )


async def get_custom_message(guild_id, msg_type):
    """Get a custom message for a specific guild and type."""
    db = Database.get_instance()
    result = await db.custom_messages.find_one(
        {"guild_id": guild_id, "type": msg_type},
        {"_id": 0, "created_at": 0, "updated_at": 0},
    )
    return result["message"] if result else None
