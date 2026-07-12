import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from guild_voice_announcements import (
    GuildVoiceAnnouncements,
    VoiceAnnouncementTarget,
    VoiceChannel,
    VoiceChannelLink,
    VoiceMember,
    VoiceStateTransition,
)


class InMemoryStore:
    def __init__(self, links, messages=None):
        self.links = links
        self.messages = messages or {}

    async def channel_link(self, voice_channel_id):
        return self.links.get(voice_channel_id)

    async def custom_message(self, guild_id, announcement_type):
        return self.messages.get((guild_id, announcement_type))


class RecordingPublisher:
    def __init__(self, targets):
        self.targets = targets
        self.messages = []

    async def target_for(self, link):
        return self.targets.get(link.text_channel_id)

    async def publish(self, target, message):
        self.messages.append((target.channel, message))


class FailingPublisher(RecordingPublisher):
    async def publish(self, target, message):
        raise RuntimeError("missing permission")


class GuildVoiceAnnouncementsTests(unittest.IsolatedAsyncioTestCase):
    async def test_announces_a_join_to_the_linked_text_channel(self):
        publisher = RecordingPublisher({"text-1": VoiceAnnouncementTarget("text-1")})
        announcements = GuildVoiceAnnouncements(
            InMemoryStore({"voice-1": VoiceChannelLink("guild-1", "text-1")}),
            publisher,
        )

        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", None, "<@1>"),
                None,
                VoiceChannel("voice-1", "Lounge"),
            )
        )

        self.assertEqual(
            [("text-1", " Alice(alice) has joined Lounge.")], publisher.messages
        )

    async def test_announces_a_leave_to_the_linked_text_channel(self):
        publisher = RecordingPublisher({"text-1": VoiceAnnouncementTarget("text-1")})
        announcements = GuildVoiceAnnouncements(
            InMemoryStore({"voice-1": VoiceChannelLink("guild-1", "text-1")}),
            publisher,
        )

        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", None, "<@1>"),
                VoiceChannel("voice-1", "Lounge"),
                None,
            )
        )

        self.assertEqual(
            [("text-1", " Alice(alice) has left Lounge.")], publisher.messages
        )

    async def test_announces_one_move_when_both_channels_share_a_text_channel(self):
        publisher = RecordingPublisher({"text-1": VoiceAnnouncementTarget("text-1")})
        announcements = GuildVoiceAnnouncements(
            InMemoryStore(
                {
                    "voice-1": VoiceChannelLink("guild-1", "text-1"),
                    "voice-2": VoiceChannelLink("guild-1", "text-1"),
                }
            ),
            publisher,
        )

        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", None, "<@1>"),
                VoiceChannel("voice-1", "Lounge"),
                VoiceChannel("voice-2", "Games"),
            )
        )

        self.assertEqual(
            [("text-1", " Alice(alice) moved from Lounge to Games.")], publisher.messages
        )

    async def test_announces_leave_and_join_when_a_move_changes_text_channels(self):
        publisher = RecordingPublisher(
            {
                "text-1": VoiceAnnouncementTarget("text-1"),
                "text-2": VoiceAnnouncementTarget("text-2"),
            }
        )
        announcements = GuildVoiceAnnouncements(
            InMemoryStore(
                {
                    "voice-1": VoiceChannelLink("guild-1", "text-1"),
                    "voice-2": VoiceChannelLink("guild-1", "text-2"),
                }
            ),
            publisher,
        )

        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", None, "<@1>"),
                VoiceChannel("voice-1", "Lounge"),
                VoiceChannel("voice-2", "Games"),
            )
        )

        self.assertEqual(
            [
                ("text-1", " Alice(alice) has left Lounge."),
                ("text-2", " Alice(alice) has joined Games."),
            ],
            publisher.messages,
        )

    async def test_applies_custom_template_tokens_and_the_target_role(self):
        publisher = RecordingPublisher(
            {"text-1": VoiceAnnouncementTarget("text-1", "<@&9>")}
        )
        announcements = GuildVoiceAnnouncements(
            InMemoryStore(
                {"voice-1": VoiceChannelLink("guild-1", "text-1", "role-9")},
                {("guild-1", "join"): "$MENTION entered $CHANNEL as $NICKNAME"},
            ),
            publisher,
        )

        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", "Ali", "<@1>"),
                None,
                VoiceChannel("voice-1", "Lounge"),
            )
        )

        self.assertEqual(
            [("text-1", "<@&9> <@1> entered Lounge as Ali")], publisher.messages
        )

    async def test_replaces_every_custom_token_in_a_move_announcement(self):
        publisher = RecordingPublisher({"text-1": VoiceAnnouncementTarget("text-1")})
        announcements = GuildVoiceAnnouncements(
            InMemoryStore(
                {
                    "voice-1": VoiceChannelLink("guild-1", "text-1"),
                    "voice-2": VoiceChannelLink("guild-1", "text-1"),
                },
                {
                    (
                        "guild-1",
                        "move",
                    ): "$USERNAME/$USER/$NICKNAME/$MENTION/$CHANNEL/$OLD_CHANNEL/$NEW_CHANNEL"
                },
            ),
            publisher,
        )

        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", None, "<@1>"),
                VoiceChannel("voice-1", "Lounge"),
                VoiceChannel("voice-2", "Games"),
            )
        )

        self.assertEqual(
            [("text-1", "alice/Alice/Alice/<@1>//Lounge/Games")], publisher.messages
        )

    async def test_skips_an_unlinked_or_unavailable_target(self):
        publisher = RecordingPublisher({})
        announcements = GuildVoiceAnnouncements(
            InMemoryStore({"voice-1": VoiceChannelLink("guild-1", "text-1")}),
            publisher,
        )

        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", None, "<@1>"),
                None,
                VoiceChannel("voice-1", "Lounge"),
            )
        )
        await announcements.announce(
            VoiceStateTransition(
                VoiceMember("alice", "Alice", None, "<@1>"),
                None,
                VoiceChannel("voice-2", "Other"),
            )
        )

        self.assertEqual([], publisher.messages)

    async def test_propagates_a_discord_send_failure(self):
        announcements = GuildVoiceAnnouncements(
            InMemoryStore({"voice-1": VoiceChannelLink("guild-1", "text-1")}),
            FailingPublisher({"text-1": VoiceAnnouncementTarget("text-1")}),
        )

        with self.assertRaisesRegex(RuntimeError, "missing permission"):
            await announcements.announce(
                VoiceStateTransition(
                    VoiceMember("alice", "Alice", None, "<@1>"),
                    None,
                    VoiceChannel("voice-1", "Lounge"),
                )
            )
