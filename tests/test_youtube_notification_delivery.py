import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from youtube_notification_delivery import (
    PublicationKind,
    YouTubeChannelObservation,
    YouTubeDeliveryStatus,
    YouTubeNotificationDelivery,
    YouTubePublication,
    YouTubeSubscription,
)


NOW = datetime(2026, 7, 13, 2, 0, tzinfo=timezone.utc)


class FakePlatform:
    def __init__(self, publications):
        self.publications = publications
        self.observed_channels = []

    async def observe(self, channel_id, cursor):
        self.observed_channels.append(channel_id)
        return YouTubeChannelObservation(
            publications=tuple(self.publications if cursor is None else ()),
            cursor="latest-playlist-item",
        )


class InMemoryStore:
    def __init__(self, subscriptions):
        self.subscriptions = tuple(subscriptions)
        self.cursors = {}
        self.deliveries = {}

    async def tracked_subscriptions(self):
        return self.subscriptions

    async def observation_cursor(self, channel_id):
        return self.cursors.get(channel_id)

    async def record_observation(self, channel_id, cursor, deliveries):
        for delivery in deliveries:
            self.deliveries.setdefault(delivery.identity, delivery)
        self.cursors[channel_id] = cursor

    async def pending_deliveries(self):
        return tuple(
            delivery for delivery in self.deliveries.values() if delivery.is_pending
        )

    async def save_delivery(self, delivery):
        self.deliveries[delivery.identity] = delivery


class RecordingPublisher:
    def __init__(self):
        self.deliveries = []

    async def publish(self, subscription, publication):
        self.deliveries.append((subscription.guild_id, publication.publication_id))
        return f"message-{len(self.deliveries)}"


class FailDeliveredResultOnceStore(InMemoryStore):
    def __init__(self, subscriptions):
        super().__init__(subscriptions)
        self.failed = False

    async def save_delivery(self, delivery):
        if delivery.delivery_status == YouTubeDeliveryStatus.DELIVERED and not self.failed:
            self.failed = True
            raise RuntimeError("Mongo unavailable after publish")
        await super().save_delivery(delivery)


class FailDispatchIntentOnceStore(InMemoryStore):
    def __init__(self, subscriptions):
        super().__init__(subscriptions)
        self.failed = False

    async def save_delivery(self, delivery):
        if delivery.delivery_status == YouTubeDeliveryStatus.DISPATCHING and not self.failed:
            self.failed = True
            raise RuntimeError("Mongo unavailable before publish")
        await super().save_delivery(delivery)


class PartiallyPersistObservationOnceStore(InMemoryStore):
    def __init__(self, subscriptions):
        super().__init__(subscriptions)
        self.failed = False

    async def record_observation(self, channel_id, cursor, deliveries):
        if not self.failed:
            self.failed = True
            first = deliveries[0]
            self.deliveries.setdefault(first.identity, first)
            raise RuntimeError("Mongo unavailable after one delivery intent")
        await super().record_observation(channel_id, cursor, deliveries)


class ChangeDestinationDuringObservationStore(InMemoryStore):
    async def record_observation(self, channel_id, cursor, deliveries):
        await super().record_observation(channel_id, cursor, deliveries)
        current = self.subscriptions[0]
        self.subscriptions = (
            YouTubeSubscription(
                current.guild_id,
                current.youtube_channel_id,
                "discord-new",
                current.subscribed_at,
            ),
        )


class FailFailureResultOnceStore(InMemoryStore):
    def __init__(self, subscriptions):
        super().__init__(subscriptions)
        self.failed = False

    async def save_delivery(self, delivery):
        if (
            delivery.delivery_status == YouTubeDeliveryStatus.PENDING
            and delivery.delivery_attempts > 0
            and not self.failed
        ):
            self.failed = True
            raise RuntimeError("Mongo unavailable after failed publish")
        await super().save_delivery(delivery)


class AlwaysFailingPublisher:
    def __init__(self):
        self.attempts = 0

    async def publish(self, subscription, publication):
        self.attempts += 1
        raise RuntimeError("Discord unavailable")


class SelectivePublisher:
    def __init__(self, failing_publication_id):
        self.failing_publication_id = failing_publication_id
        self.deliveries = []

    async def publish(self, subscription, publication):
        if publication.publication_id == self.failing_publication_id:
            raise RuntimeError("One publication failed")
        self.deliveries.append(publication.publication_id)
        return f"message-{publication.publication_id}"


class DestinationPublisher:
    def __init__(self):
        self.destinations = []

    async def publish(self, subscription, publication):
        self.destinations.append(subscription.notification_channel_id)
        if len(self.destinations) == 1:
            raise RuntimeError("First destination failed")
        return "message-1"


class RecordingDestinationPublisher:
    def __init__(self):
        self.destinations = []

    async def publish(self, subscription, publication):
        self.destinations.append(subscription.notification_channel_id)
        return "message-1"


class YouTubeNotificationDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_one_publication_independently_to_every_subscribed_guild(self):
        subscriptions = [
            YouTubeSubscription("guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)),
            YouTubeSubscription("guild-b", "channel-1", "discord-b", NOW - timedelta(days=1)),
        ]
        publication = YouTubePublication(
            publication_id="video-1",
            channel_id="channel-1",
            title="Architecture live stream",
            url="https://www.youtube.com/watch?v=video-1",
            thumbnail_url="https://img.example/video-1.jpg",
            channel_name="Example Channel",
            published_at=NOW,
            kind=PublicationKind.LIVE,
        )
        platform = FakePlatform([publication])
        store = InMemoryStore(subscriptions)
        publisher = RecordingPublisher()
        deliveries = YouTubeNotificationDelivery(platform, store, publisher)

        await deliveries.poll()
        await deliveries.poll()

        self.assertEqual(
            publisher.deliveries,
            [("guild-a", "video-1"), ("guild-b", "video-1")],
        )
        self.assertEqual(platform.observed_channels, ["channel-1", "channel-1"])

    async def test_abandons_a_delivery_after_four_failed_attempts(self):
        subscription = YouTubeSubscription(
            "guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)
        )
        publication = YouTubePublication(
            publication_id="video-1",
            channel_id="channel-1",
            title="Unavailable publication",
            url="https://www.youtube.com/watch?v=video-1",
            thumbnail_url="",
            channel_name="Example Channel",
            published_at=NOW,
        )
        store = InMemoryStore([subscription])
        publisher = AlwaysFailingPublisher()
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([publication]), store, publisher
        )

        for _ in range(4):
            await deliveries.poll()

        self.assertEqual(publisher.attempts, 4)

    async def test_does_not_resend_when_the_delivered_result_cannot_be_saved(self):
        subscription = YouTubeSubscription(
            "guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)
        )
        publication = YouTubePublication(
            publication_id="video-1",
            channel_id="channel-1",
            title="One notification only",
            url="https://www.youtube.com/watch?v=video-1",
            thumbnail_url="",
            channel_name="Example Channel",
            published_at=NOW,
        )
        store = FailDeliveredResultOnceStore([subscription])
        publisher = RecordingPublisher()
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([publication]), store, publisher
        )

        await deliveries.poll()
        await deliveries.poll()

        self.assertEqual(publisher.deliveries, [("guild-a", "video-1")])

    async def test_failed_publication_does_not_block_a_newer_publication(self):
        subscription = YouTubeSubscription(
            "guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)
        )
        old = YouTubePublication(
            "video-old", "channel-1", "Old", "old-url", "", "Example", NOW
        )
        new = YouTubePublication(
            "video-new",
            "channel-1",
            "New",
            "new-url",
            "",
            "Example",
            NOW + timedelta(minutes=1),
        )
        publisher = SelectivePublisher("video-old")
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([new, old]), InMemoryStore([subscription]), publisher
        )

        await deliveries.poll()

        self.assertEqual(publisher.deliveries, ["video-new"])

    async def test_failed_result_persistence_does_not_block_a_newer_publication(self):
        subscription = YouTubeSubscription(
            "guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)
        )
        old = YouTubePublication(
            "video-old", "channel-1", "Old", "old-url", "", "Example", NOW
        )
        new = YouTubePublication(
            "video-new",
            "channel-1",
            "New",
            "new-url",
            "",
            "Example",
            NOW + timedelta(minutes=1),
        )
        publisher = SelectivePublisher("video-old")
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([old, new]),
            FailFailureResultOnceStore([subscription]),
            publisher,
        )

        await deliveries.poll()

        self.assertEqual(publisher.deliveries, ["video-new"])

    async def test_retry_uses_the_subscriptions_current_destination(self):
        original = YouTubeSubscription(
            "guild-a", "channel-1", "discord-old", NOW - timedelta(days=1)
        )
        publication = YouTubePublication(
            "video-1", "channel-1", "Move", "url", "", "Example", NOW
        )
        store = InMemoryStore([original])
        publisher = DestinationPublisher()
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([publication]), store, publisher
        )

        await deliveries.poll()
        store.subscriptions = (
            YouTubeSubscription(
                "guild-a", "channel-1", "discord-new", NOW - timedelta(days=1)
            ),
        )
        await deliveries.poll()

        self.assertEqual(publisher.destinations, ["discord-old", "discord-new"])

    async def test_initial_dispatch_refreshes_the_subscriptions_destination(self):
        original = YouTubeSubscription(
            "guild-a", "channel-1", "discord-old", NOW - timedelta(days=1)
        )
        publication = YouTubePublication(
            "video-1", "channel-1", "Move now", "url", "", "Example", NOW
        )
        publisher = RecordingDestinationPublisher()
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([publication]),
            ChangeDestinationDuringObservationStore([original]),
            publisher,
        )

        await deliveries.poll()

        self.assertEqual(publisher.destinations[-1], "discord-new")

    async def test_subscription_removal_stops_pending_retries(self):
        subscription = YouTubeSubscription(
            "guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)
        )
        publication = YouTubePublication(
            "video-1", "channel-1", "Stop", "url", "", "Example", NOW
        )
        store = InMemoryStore([subscription])
        publisher = AlwaysFailingPublisher()
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([publication]), store, publisher
        )

        await deliveries.poll()
        store.subscriptions = ()
        await deliveries.poll()

        self.assertEqual(publisher.attempts, 1)

    async def test_reobserves_after_only_some_delivery_intents_are_created(self):
        subscriptions = [
            YouTubeSubscription(
                "guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)
            ),
            YouTubeSubscription(
                "guild-b", "channel-1", "discord-b", NOW - timedelta(days=1)
            ),
        ]
        publication = YouTubePublication(
            publication_id="video-1",
            channel_id="channel-1",
            title="Do not skip me",
            url="https://www.youtube.com/watch?v=video-1",
            thumbnail_url="",
            channel_name="Example Channel",
            published_at=NOW,
        )
        store = PartiallyPersistObservationOnceStore(subscriptions)
        publisher = RecordingPublisher()
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([publication]), store, publisher
        )

        await deliveries.poll()
        await deliveries.poll()

        self.assertEqual(
            publisher.deliveries,
            [("guild-a", "video-1"), ("guild-b", "video-1")],
        )

    async def test_waits_to_publish_until_dispatching_intent_is_saved(self):
        subscription = YouTubeSubscription(
            "guild-a", "channel-1", "discord-a", NOW - timedelta(days=1)
        )
        publication = YouTubePublication(
            publication_id="video-1",
            channel_id="channel-1",
            title="Persist before publish",
            url="https://www.youtube.com/watch?v=video-1",
            thumbnail_url="",
            channel_name="Example Channel",
            published_at=NOW,
        )
        store = FailDispatchIntentOnceStore([subscription])
        publisher = RecordingPublisher()
        deliveries = YouTubeNotificationDelivery(
            FakePlatform([publication]), store, publisher
        )

        await deliveries.poll()
        await deliveries.poll()

        self.assertEqual(publisher.deliveries, [("guild-a", "video-1")])


if __name__ == "__main__":
    unittest.main()
