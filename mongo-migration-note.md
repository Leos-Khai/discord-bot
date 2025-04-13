# MongoDB Migration Guide

## Overview

This guide details the steps required to migrate the Discord bot from SQLite to MongoDB. The migration will improve scalability and provide better support for distributed deployments.

## Prerequisites

- MongoDB 7.0 or higher
- Python packages to add to `requirements.txt`:
  
  ```text
  pymongo==4.6.2
  motor==3.3.2  # Async MongoDB driver
  ```

## Database Schema Changes

### Current SQLite Schema to MongoDB Collections

1. **servers Collection**

```javascript
{
  _id: ObjectId,
  server_id: String,  // was INTEGER PRIMARY KEY in SQLite
  created_at: ISODate
}
```

1. **channel_links Collection**

```javascript
{
  _id: ObjectId,
  guild_id: String,
  text_channel_id: String,
  voice_channel_id: String,  // Create unique index
  role_id: String,
  created_at: ISODate,
  updated_at: ISODate
}
```

1. **custom_messages Collection**

```javascript
{
  _id: ObjectId,
  guild_id: String,
  type: String,  // "join", "leave", or "move"
  message: String,
  created_at: ISODate,
  updated_at: ISODate
}
// Create compound unique index on [guild_id, type]
```

## Implementation Steps

1. **Create New MongoDB Module**

Create `src/mongodb.py`:

```python
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime

class MongoDB:
    def __init__(self, connection_string):
        self.client = AsyncIOMotorClient(connection_string)
        self.db = self.client.discord_bot
        
        # Collections
        self.servers = self.db.servers
        self.channel_links = self.db.channel_links
        self.custom_messages = self.db.custom_messages

    async def init_indexes(self):
        # Create indexes
        await self.channel_links.create_index("voice_channel_id", unique=True)
        await self.custom_messages.create_index(
            [("guild_id", 1), ("type", 1)], 
            unique=True
        )

    # Server operations
    async def add_server(self, server_id: str):
        await self.servers.update_one(
            {"server_id": server_id},
            {
                "$setOnInsert": {
                    "server_id": server_id,
                    "created_at": datetime.utcnow()
                }
            },
            upsert=True
        )

    async def get_servers(self):
        return await self.servers.find().to_list(None)

    # Channel link operations
    async def add_channel_link(self, guild_id, text_channel_id, voice_channel_id, role_id=None):
        now = datetime.utcnow()
        await self.channel_links.insert_one({
            "guild_id": guild_id,
            "text_channel_id": text_channel_id,
            "voice_channel_id": voice_channel_id,
            "role_id": role_id,
            "created_at": now,
            "updated_at": now
        })

    async def get_channel_link(self, voice_channel_id):
        return await self.channel_links.find_one({"voice_channel_id": voice_channel_id})

    # Custom message operations
    async def set_custom_message(self, guild_id, msg_type, message):
        now = datetime.utcnow()
        if message is None:
            await self.custom_messages.delete_one({
                "guild_id": guild_id,
                "type": msg_type
            })
        else:
            await self.custom_messages.update_one(
                {"guild_id": guild_id, "type": msg_type},
                {
                    "$set": {
                        "message": message,
                        "updated_at": now
                    },
                    "$setOnInsert": {
                        "created_at": now
                    }
                },
                upsert=True
            )
```

1. **Update Configuration**

Add to `config.json`:

```json
{
  "mongodb": {
    "uri": "mongodb://localhost:27017",
    "database": "discord_bot"
  }
}
```

1. **Update `main.py`**

- Import and initialize MongoDB
- Replace SQLite initialization with MongoDB

1. **Update All Cogs**

- Convert all database operations to use async/await
- Update error handling for MongoDB specific errors
- Replace SQLite queries with MongoDB operations

## Data Migration Script

Create `scripts/migrate_to_mongo.py`:

```python
import asyncio
import sqlite3
from mongodb import MongoDB
from datetime import datetime

async def migrate_data():
    # Connect to SQLite
    sqlite_conn = sqlite3.connect('src/discord_bot.db')
    sqlite_cur = sqlite_conn.cursor()
    
    # Connect to MongoDB
    mongo = MongoDB("mongodb://localhost:27017")
    
    try:
        # Migrate servers
        sqlite_cur.execute("SELECT * FROM servers")
        for row in sqlite_cur.fetchall():
            await mongo.servers.insert_one({
                "server_id": row[1],
                "created_at": datetime.utcnow()
            })
        
        # Migrate channel links
        sqlite_cur.execute("SELECT * FROM channel_links")
        for row in sqlite_cur.fetchall():
            await mongo.channel_links.insert_one({
                "guild_id": row[1],
                "text_channel_id": row[2],
                "voice_channel_id": row[3],
                "role_id": row[4],
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            })
        
        # Migrate custom messages
        sqlite_cur.execute("SELECT * FROM custom_messages")
        for row in sqlite_cur.fetchall():
            await mongo.custom_messages.insert_one({
                "guild_id": row[1],
                "type": row[2],
                "message": row[3],
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            })
            
    finally:
        sqlite_conn.close()

if __name__ == "__main__":
    asyncio.run(migrate_data())
```

## Docker Updates

Update `Dockerfile`:

```dockerfile
# Add MongoDB client tools
RUN apk add --no-cache mongodb-tools

# Add healthcheck for MongoDB connection
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from mongodb import MongoDB; MongoDB('${MONGODB_URI}').client.admin.command('ping')"
```

## Environment Variables

Add to deployment:

```bash
MONGODB_URI=mongodb://username:password@host:port/database
MONGODB_DATABASE=discord_bot
```

## Testing

1. Create a test MongoDB instance
2. Run the migration script
3. Verify data integrity
4. Test all bot commands
5. Monitor performance and error rates

## Rollback Plan

1. Keep SQLite database as backup
2. Maintain ability to switch back to SQLite
3. Document all changes for potential rollback

## Performance Considerations

1. Implement connection pooling
2. Use appropriate indexes
3. Monitor query performance
4. Implement caching if needed

## Security Notes

1. Use MongoDB authentication
2. Enable TLS/SSL
3. Set up proper access controls
4. Regular security audits

## Monitoring

1. Set up MongoDB monitoring
2. Configure alerts
3. Monitor performance metrics
4. Log all critical operations
