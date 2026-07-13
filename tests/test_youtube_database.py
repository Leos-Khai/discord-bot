import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db import DatabaseError, DatabaseService
from youtube_notification_delivery import YouTubeDeliveryState, YouTubePublication


NOW = datetime(2026, 7, 13, 2, 0, tzinfo=timezone.utc)


class MissingDeliveryCollection:
    async def update_one(self, query, update):
        return type("UpdateResult", (), {"matched_count": 0})()


class SubscriptionCollection:
    def __init__(self, *, deleted_count=0, remaining=0):
        self.inserted = None
        self.deleted_count = deleted_count
        self.remaining = remaining

    async def insert_one(self, document):
        self.inserted = document

    async def delete_one(self, query):
        return type("DeleteResult", (), {"deleted_count": self.deleted_count})()

    async def count_documents(self, query, **kwargs):
        return self.remaining


class ObservationCollection:
    def __init__(self):
        self.updated = None
        self.deleted = None

    async def update_one(self, query, update, *, upsert=False):
        self.updated = (query, update, upsert)

    async def delete_one(self, query):
        self.deleted = query


class DeliveryCollection:
    def __init__(self):
        self.deleted = None

    async def delete_many(self, query):
        self.deleted = query


class InitializableCollection:
    def __init__(self, distinct_values=()):
        self.distinct_values = list(distinct_values)
        self.updates = []

    async def create_index(self, *args, **kwargs):
        return None

    async def distinct(self, field):
        return self.distinct_values

    async def update_one(self, query, update, *, upsert=False):
        self.updates.append((query, update, upsert))


class YouTubeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_a_dispatch_intent_when_the_delivery_was_removed(self):
        database = object.__new__(DatabaseService)
        database.youtube_delivery_states = MissingDeliveryCollection()
        delivery = YouTubeDeliveryState(
            guild_id="guild-1",
            youtube_channel_id="channel-1",
            publication=YouTubePublication(
                "video-1", "channel-1", "Removed", "url", "", "Example", NOW
            ),
        )

        with self.assertRaises(DatabaseError):
            await database.save_youtube_delivery(delivery)

    async def test_new_subscription_establishes_a_no_replay_baseline(self):
        database = object.__new__(DatabaseService)
        database.youtube_subscriptions = SubscriptionCollection()
        database.youtube_channel_observations = ObservationCollection()

        await database.add_youtube_subscription(
            "guild-1", "channel-1", "discord-1", "Example Channel"
        )

        query, update, upsert = database.youtube_channel_observations.updated
        self.assertEqual(
            (query, upsert, update["$setOnInsert"]["youtube_channel_id"]),
            ({"youtube_channel_id": "channel-1"}, True, "channel-1"),
        )

    async def test_initialization_baselines_existing_subscriptions_without_replay(self):
        database = object.__new__(DatabaseService)
        collection_names = (
            "channel_links",
            "custom_messages",
            "music_channel_limits",
            "guild_playback_settings",
            "user_tts_voices",
            "notification_channels",
            "youtube_channel_observations",
            "youtube_delivery_states",
            "twitch_subscriptions",
            "twitch_stream_status",
            "youtube_channel_meta",
            "twitch_user_meta",
        )
        for name in collection_names:
            setattr(database, name, InitializableCollection())
        database.youtube_subscriptions = InitializableCollection(["channel-1"])

        await database.initialize()

        query, update, upsert = database.youtube_channel_observations.updates[0]
        self.assertEqual(
            (query, upsert, update["$setOnInsert"]["youtube_channel_id"]),
            ({"youtube_channel_id": "channel-1"}, True, "channel-1"),
        )

    async def test_removing_the_last_subscription_cleans_its_delivery_history(self):
        database = object.__new__(DatabaseService)
        database.youtube_subscriptions = SubscriptionCollection(
            deleted_count=1, remaining=0
        )
        database.youtube_delivery_states = DeliveryCollection()
        database.youtube_channel_observations = ObservationCollection()

        removed = await database.remove_youtube_subscription("guild-1", "channel-1")

        self.assertEqual(
            (
                removed,
                database.youtube_delivery_states.deleted,
                database.youtube_channel_observations.deleted,
            ),
            (
                True,
                {"guild_id": "guild-1", "youtube_channel_id": "channel-1"},
                {"youtube_channel_id": "channel-1"},
            ),
        )


if __name__ == "__main__":
    unittest.main()
