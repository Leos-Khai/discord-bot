import asyncio
import sqlite3
import os
import sys
import json

# Add the src directory to the Python path so we can import the db module
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
from db import Database


async def migrate_data():
    """Migrate data from SQLite to MongoDB."""
    print("Starting migration from SQLite to MongoDB...")

    # Connect to SQLite
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sqlite_path = os.path.join(script_dir, "..", "src", "discord_bot.db")

    if not os.path.exists(sqlite_path):
        print(f"SQLite database not found at {sqlite_path}")
        return

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_cur = sqlite_conn.cursor()

    # Get MongoDB instance
    db = Database.get_instance()

    try:
        # Migrate servers
        print("Migrating servers...")
        sqlite_cur.execute("SELECT * FROM servers")
        for row in sqlite_cur.fetchall():
            await db.servers.update_one(
                {"server_id": row[1]},
                {
                    "$setOnInsert": {
                        "server_id": row[1],
                        "created_at": datetime.utcnow(),
                    }
                },
                upsert=True,
            )
        print("Servers migration complete.")

        # Migrate channel links
        print("Migrating channel links...")
        sqlite_cur.execute("SELECT * FROM channel_links")
        for row in sqlite_cur.fetchall():
            try:
                await db.channel_links.insert_one(
                    {
                        "guild_id": row[1],
                        "text_channel_id": row[2],
                        "voice_channel_id": row[3],
                        "role_id": row[4],
                        "created_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }
                )
            except Exception as e:
                print(f"Error migrating channel link: {e}")
        print("Channel links migration complete.")

        # Migrate custom messages
        print("Migrating custom messages...")
        sqlite_cur.execute("SELECT * FROM custom_messages")
        for row in sqlite_cur.fetchall():
            try:
                await db.custom_messages.update_one(
                    {"guild_id": row[1], "type": row[2]},
                    {
                        "$set": {"message": row[3], "updated_at": datetime.utcnow()},
                        "$setOnInsert": {"created_at": datetime.utcnow()},
                    },
                    upsert=True,
                )
            except Exception as e:
                print(f"Error migrating custom message: {e}")
        print("Custom messages migration complete.")

    finally:
        print("Closing SQLite connection...")
        sqlite_conn.close()

    print("Migration completed successfully!")


if __name__ == "__main__":
    from datetime import datetime

    asyncio.run(migrate_data())
