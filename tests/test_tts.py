import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from guild_command_access import GuildCommandChannelAccess
from tts import DEFAULT_TTS_VOICE, TtsPreferences, TtsQueue


class FakePreferencesStore:
    def __init__(self):
        self.values = {}

    async def get_tts_voice(self, guild_id, user_id):
        return self.values.get((guild_id, user_id))

    async def set_tts_voice(self, guild_id, user_id, voice):
        self.values[(guild_id, user_id)] = voice


class FakeCommandChannelStore:
    async def get_command_channels(self, guild_id):
        return []


class TtsPreferencesTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_the_universal_default_until_a_user_selects_a_guild_voice(self):
        preferences = TtsPreferences(FakePreferencesStore())

        self.assertEqual(DEFAULT_TTS_VOICE, await preferences.voice_for("guild-1", "user-1"))

        await preferences.set_voice("guild-1", "user-1", "en-GB-SoniaNeural")

        self.assertEqual("en-GB-SoniaNeural", await preferences.voice_for("guild-1", "user-1"))
        self.assertEqual(DEFAULT_TTS_VOICE, await preferences.voice_for("guild-2", "user-1"))


class FakeSpeaker:
    def __init__(self):
        self.requests = []
        self.completed = []

    async def speak(self, request, complete):
        self.requests.append(request)
        self.completed.append(complete)

    def finish(self, error=None):
        self.completed.pop(0)(error)


class TtsQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_requests_and_rejects_a_second_pending_request_from_the_same_user(self):
        clock = [100.0]
        speaker = FakeSpeaker()
        queue = TtsQueue(speaker, clock=lambda: clock[0])

        first = await queue.enqueue("user-1", "voice-1", "first")
        second = await queue.enqueue("user-2", "voice-2", "second")
        duplicate = await queue.enqueue("user-1", "voice-1", "again")

        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertFalse(duplicate.accepted)
        self.assertEqual("already have a TTS message pending", duplicate.detail)
        self.assertEqual(["first"], [request.message for request in speaker.requests])

        speaker.finish()
        await self._flush()

        self.assertEqual(["first", "second"], [request.message for request in speaker.requests])

    async def test_rejects_messages_over_the_limit_and_enforces_the_user_cooldown(self):
        clock = [100.0]
        speaker = FakeSpeaker()
        queue = TtsQueue(speaker, clock=lambda: clock[0])

        too_long = await queue.enqueue("user-1", "voice-1", "x" * 301)
        first = await queue.enqueue("user-1", "voice-1", "first")
        speaker.finish()
        await self._flush()
        clock[0] = 103.0
        cooldown = await queue.enqueue("user-1", "voice-1", "second")
        clock[0] = 105.0
        allowed = await queue.enqueue("user-1", "voice-1", "third")

        self.assertFalse(too_long.accepted)
        self.assertEqual("Messages are limited to 300 characters", too_long.detail)
        self.assertTrue(first.accepted)
        self.assertFalse(cooldown.accepted)
        self.assertEqual("Please wait 2 more second(s) before using TTS again", cooldown.detail)
        self.assertTrue(allowed.accepted)

    async def _flush(self):
        await __import__("asyncio").sleep(0)
        await __import__("asyncio").sleep(0)


class FakeInteractionResponse:
    def __init__(self):
        self.messages = []

    def is_done(self):
        return False

    async def send_message(self, message, *, ephemeral):
        self.messages.append((message, ephemeral))


class FakeInteraction:
    def __init__(self):
        self.guild = type("Guild", (), {"id": 1})()
        self.channel_id = 2
        self.response = FakeInteractionResponse()


class TtsCommandChecksTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_commands_require_a_configured_music_channel(self):
        from cogs.tts import TtsCommands

        cog = object.__new__(TtsCommands)
        cog.command_access = GuildCommandChannelAccess(FakeCommandChannelStore())
        interaction = FakeInteraction()
        allowed = await cog.interaction_check(interaction)

        self.assertFalse(allowed)
        self.assertEqual(
            [
                (
                    "An administrator must configure command channels before Music or TTS can be used.",
                    True,
                )
            ],
            interaction.response.messages,
        )
