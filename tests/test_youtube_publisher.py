import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cogs.notifications import DiscordYouTubePublisher
from youtube_notification_delivery import (
    PublicationKind,
    YouTubePublication,
    YouTubeSubscription,
)


NOW = datetime(2026, 7, 13, 2, 0, tzinfo=timezone.utc)


class RecordingChannel:
    def __init__(self):
        self.embed = None

    async def send(self, *, embed):
        self.embed = embed
        return type("Message", (), {"id": 42})()


class FakeBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 123 else None


class DiscordYouTubePublisherTests(unittest.IsolatedAsyncioTestCase):
    async def test_labels_a_confirmed_live_publication(self):
        channel = RecordingChannel()
        publisher = DiscordYouTubePublisher(FakeBot(channel))
        subscription = YouTubeSubscription(
            "guild-1", "youtube-1", "123", NOW - timedelta(days=1)
        )
        publication = YouTubePublication(
            "video-1",
            "youtube-1",
            "Live architecture review",
            "https://www.youtube.com/watch?v=video-1",
            "https://img.example/video-1.jpg",
            "Example Channel",
            NOW,
            PublicationKind.LIVE,
        )

        message_id = await publisher.publish(subscription, publication)

        self.assertEqual(
            (message_id, channel.embed.author.name, channel.embed.fields[0].value),
            ("42", "Example Channel is live!", "Live"),
        )

    async def test_labels_upcoming_and_archived_streams(self):
        cases = [
            (
                PublicationKind.UPCOMING_STREAM,
                "Example Channel scheduled a stream!",
                "Upcoming stream",
            ),
            (
                PublicationKind.ARCHIVED_STREAM,
                "Example Channel published a stream recording!",
                "Archived stream",
            ),
        ]
        for kind, author, label in cases:
            with self.subTest(kind=kind):
                channel = RecordingChannel()
                publication = YouTubePublication(
                    "video-1",
                    "youtube-1",
                    "Stream",
                    "https://www.youtube.com/watch?v=video-1",
                    "",
                    "Example Channel",
                    NOW,
                    kind,
                )

                await DiscordYouTubePublisher(FakeBot(channel)).publish(
                    YouTubeSubscription(
                        "guild-1", "youtube-1", "123", NOW - timedelta(days=1)
                    ),
                    publication,
                )

                self.assertEqual(
                    (channel.embed.author.name, channel.embed.fields[0].value),
                    (author, label),
                )


if __name__ == "__main__":
    unittest.main()
