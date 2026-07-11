import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from twitch_transitions import (
    DeliveryStatus,
    TwitchDeliveryState,
    TwitchStream,
    TwitchSubscription,
    TwitchTransitions,
)


class FakePlatform:
    def __init__(self, streams, vod_url=None):
        self.streams = streams
        self._vod_url = vod_url

    async def live_streams(self, usernames):
        return self.streams

    async def vod_url(self, user_id, stream_id, user_login):
        return self._vod_url


class SequencedPlatform:
    def __init__(self, *observations, vod_url=None):
        self.observations = list(observations)
        self._vod_url = vod_url

    async def live_streams(self, usernames):
        return self.observations.pop(0)

    async def vod_url(self, user_id, stream_id, user_login):
        return self._vod_url


class FakeStore:
    def __init__(self, subscriptions, failing_saves=()):
        self.subscriptions = subscriptions
        self.states = {}
        self.failing_saves = set(failing_saves)
        self.save_attempts = 0

    async def tracked_subscriptions(self):
        return self.subscriptions

    async def delivery_state(self, guild_id, username):
        return self.states.get((guild_id, username))

    async def save_delivery_state(self, state):
        self.save_attempts += 1
        if self.save_attempts in self.failing_saves:
            raise RuntimeError("MongoDB unavailable")
        self.states[(state.guild_id, state.twitch_username)] = state


class FakePublisher:
    def __init__(self, fail_live=False, fail_offline=False):
        self.live = []
        self.offline = []
        self.fail_live = fail_live
        self.fail_offline = fail_offline

    async def publish_live(self, subscription, stream):
        self.live.append((subscription, stream))
        if self.fail_live:
            raise RuntimeError("Discord unavailable")
        return "message-1"

    async def publish_offline(self, subscription, state, vod_url):
        self.offline.append((subscription, state, vod_url))
        if self.fail_offline:
            raise RuntimeError("Discord unavailable")
        return "message-2"


class TwitchTransitionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivers_a_new_live_stream_once(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1", "Streamer")
        stream = TwitchStream(
            stream_id="stream-1",
            user_id="user-1",
            user_login="streamer",
            display_name="Streamer",
            payload={"id": "stream-1", "user_login": "streamer"},
        )
        store = FakeStore([subscription])
        publisher = FakePublisher()
        transitions = TwitchTransitions(FakePlatform([stream]), store, publisher)

        await transitions.poll()

        self.assertEqual(1, len(publisher.live))
        state = store.states[("guild-1", "streamer")]
        self.assertTrue(state.is_live)
        self.assertEqual("stream-1", state.stream_id)
        self.assertEqual("message-1", state.message_id)
        self.assertEqual(DeliveryStatus.DELIVERED, state.delivery_status)

    async def test_records_a_failed_live_delivery_as_pending(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        stream = TwitchStream(
            stream_id="stream-1",
            user_id="user-1",
            user_login="streamer",
            display_name="Streamer",
            payload={"id": "stream-1", "user_login": "streamer"},
        )
        store = FakeStore([subscription])
        publisher = FakePublisher(fail_live=True)
        transitions = TwitchTransitions(FakePlatform([stream]), store, publisher)

        await transitions.poll()

        state = store.states[("guild-1", "streamer")]
        self.assertTrue(state.is_live)
        self.assertEqual(DeliveryStatus.PENDING, state.delivery_status)
        self.assertEqual(1, state.delivery_attempts)

    async def test_abandons_a_live_delivery_after_three_retries(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        stream = TwitchStream(
            stream_id="stream-1",
            user_id="user-1",
            user_login="streamer",
            display_name="Streamer",
            payload={"id": "stream-1", "user_login": "streamer"},
        )
        store = FakeStore([subscription])
        publisher = FakePublisher(fail_live=True)
        transitions = TwitchTransitions(FakePlatform([stream]), store, publisher)

        for _ in range(4):
            await transitions.poll()

        self.assertEqual(4, len(publisher.live))
        state = store.states[("guild-1", "streamer")]
        self.assertEqual(DeliveryStatus.ABANDONED, state.delivery_status)
        self.assertEqual(4, state.delivery_attempts)

    async def test_delivers_an_offline_transition_after_live_delivery(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        store = FakeStore([subscription])
        store.states[("guild-1", "streamer")] = TwitchDeliveryState(
            guild_id="guild-1",
            twitch_username="streamer",
            is_live=True,
            stream_id="stream-1",
            message_id="message-1",
            delivery_status=DeliveryStatus.DELIVERED,
            delivery_attempts=1,
            user_id="user-1",
            user_login="streamer",
            display_name="Streamer",
        )
        publisher = FakePublisher()
        transitions = TwitchTransitions(SequencedPlatform([]), store, publisher)

        await transitions.poll()

        self.assertEqual(1, len(publisher.offline))
        state = store.states[("guild-1", "streamer")]
        self.assertFalse(state.is_live)
        self.assertEqual("message-2", state.message_id)
        self.assertEqual(DeliveryStatus.DELIVERED, state.delivery_status)

    async def test_passes_the_platform_vod_to_the_offline_publisher(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        store = FakeStore([subscription])
        store.states[("guild-1", "streamer")] = TwitchDeliveryState(
            guild_id="guild-1",
            twitch_username="streamer",
            is_live=True,
            stream_id="stream-1",
            message_id="message-1",
            delivery_status=DeliveryStatus.DELIVERED,
            delivery_attempts=1,
            user_id="user-1",
            user_login="streamer",
        )
        publisher = FakePublisher()
        transitions = TwitchTransitions(
            FakePlatform([], vod_url="https://twitch.tv/videos/1"), store, publisher
        )

        await transitions.poll()

        self.assertEqual("https://twitch.tv/videos/1", publisher.offline[0][2])

    async def test_abandons_an_undelivered_live_notice_when_the_stream_ends(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        store = FakeStore([subscription])
        store.states[("guild-1", "streamer")] = TwitchDeliveryState(
            guild_id="guild-1",
            twitch_username="streamer",
            is_live=True,
            stream_id="stream-1",
            message_id=None,
            delivery_status=DeliveryStatus.PENDING,
            delivery_attempts=2,
        )
        publisher = FakePublisher()
        transitions = TwitchTransitions(SequencedPlatform([]), store, publisher)

        await transitions.poll()

        self.assertEqual([], publisher.offline)
        state = store.states[("guild-1", "streamer")]
        self.assertFalse(state.is_live)
        self.assertEqual(DeliveryStatus.ABANDONED, state.delivery_status)

    async def test_abandons_an_offline_delivery_after_three_retries(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        store = FakeStore([subscription])
        store.states[("guild-1", "streamer")] = TwitchDeliveryState(
            guild_id="guild-1",
            twitch_username="streamer",
            is_live=True,
            stream_id="stream-1",
            message_id="message-1",
            delivery_status=DeliveryStatus.DELIVERED,
            delivery_attempts=1,
        )
        publisher = FakePublisher(fail_offline=True)
        transitions = TwitchTransitions(SequencedPlatform([], [], [], []), store, publisher)

        for _ in range(4):
            await transitions.poll()

        self.assertEqual(4, len(publisher.offline))
        state = store.states[("guild-1", "streamer")]
        self.assertFalse(state.is_live)
        self.assertEqual(DeliveryStatus.ABANDONED, state.delivery_status)
        self.assertEqual(4, state.delivery_attempts)

    async def test_does_not_resend_a_live_delivery_left_dispatching_by_a_restart(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        stream = TwitchStream(
            stream_id="stream-1",
            user_id="user-1",
            user_login="streamer",
            display_name="Streamer",
            payload={"id": "stream-1", "user_login": "streamer"},
        )
        store = FakeStore([subscription])
        store.states[("guild-1", "streamer")] = TwitchDeliveryState(
            guild_id="guild-1",
            twitch_username="streamer",
            is_live=True,
            stream_id="stream-1",
            message_id=None,
            delivery_status=DeliveryStatus.DISPATCHING,
            delivery_attempts=1,
        )
        publisher = FakePublisher()
        transitions = TwitchTransitions(FakePlatform([stream]), store, publisher)

        await transitions.poll()

        self.assertEqual([], publisher.live)
        state = store.states[("guild-1", "streamer")]
        self.assertEqual(DeliveryStatus.ABANDONED, state.delivery_status)

    async def test_does_not_advance_delivery_state_when_platform_observation_fails(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        store = FakeStore([subscription])
        original_state = TwitchDeliveryState(
            guild_id="guild-1",
            twitch_username="streamer",
            is_live=True,
            stream_id="stream-1",
            message_id=None,
            delivery_status=DeliveryStatus.PENDING,
            delivery_attempts=1,
        )
        store.states[("guild-1", "streamer")] = original_state
        publisher = FakePublisher()
        transitions = TwitchTransitions(FakePlatform(None), store, publisher)

        await transitions.poll()

        self.assertEqual(original_state, store.states[("guild-1", "streamer")])
        self.assertEqual([], publisher.live)
        self.assertEqual([], publisher.offline)

    async def test_does_not_duplicate_a_delivered_message_when_its_state_write_fails(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        stream = TwitchStream(
            stream_id="stream-1",
            user_id="user-1",
            user_login="streamer",
            display_name="Streamer",
            payload={"id": "stream-1", "user_login": "streamer"},
        )
        store = FakeStore([subscription], failing_saves={2})
        publisher = FakePublisher()
        transitions = TwitchTransitions(FakePlatform([stream]), store, publisher)

        await transitions.poll()
        await transitions.poll()

        self.assertEqual(1, len(publisher.live))
        state = store.states[("guild-1", "streamer")]
        self.assertEqual(DeliveryStatus.DELIVERED, state.delivery_status)
        self.assertEqual("message-1", state.message_id)

    async def test_waits_to_send_until_the_dispatching_intent_is_persisted(self):
        subscription = TwitchSubscription("guild-1", "streamer", "channel-1")
        stream = TwitchStream(
            stream_id="stream-1",
            user_id="user-1",
            user_login="streamer",
            display_name="Streamer",
            payload={"id": "stream-1", "user_login": "streamer"},
        )
        store = FakeStore([subscription], failing_saves={1})
        publisher = FakePublisher()
        transitions = TwitchTransitions(FakePlatform([stream]), store, publisher)

        await transitions.poll()
        await transitions.poll()

        self.assertEqual(1, len(publisher.live))
        state = store.states[("guild-1", "streamer")]
        self.assertEqual(1, state.delivery_attempts)
