import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from guild_command_access import GuildCommandChannelAccess, UNCONFIGURED_MESSAGE


class InMemoryCommandChannelStore:
    def __init__(self):
        self.channels: dict[str, list[str]] = {}

    async def get_command_channels(self, guild_id: str) -> list[str]:
        return self.channels.get(guild_id, [])

    async def add_command_channel(self, guild_id: str, channel_id: str) -> list[str]:
        channels = self.channels.setdefault(guild_id, [])
        if channel_id not in channels:
            channels.append(channel_id)
        return channels

    async def remove_command_channel(self, guild_id: str, channel_id: str) -> list[str]:
        self.channels[guild_id] = [
            candidate
            for candidate in self.channels.get(guild_id, [])
            if candidate != channel_id
        ]
        return self.channels[guild_id]

    async def clear_command_channels(self, guild_id: str) -> list[str]:
        self.channels[guild_id] = []
        return []


class GuildCommandChannelAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_denies_commands_until_an_administrator_configures_a_channel(self):
        access = GuildCommandChannelAccess(InMemoryCommandChannelStore())

        decision = await access.evaluate("guild-1", "channel-1")

        self.assertFalse(decision.allowed)
        self.assertEqual(
            "An administrator must configure command channels before Music or TTS can be used.",
            decision.detail,
        )

    async def test_applies_administrator_changes_immediately(self):
        access = GuildCommandChannelAccess(InMemoryCommandChannelStore())

        await access.allow("guild-1", ["channel-1", "channel-1"])
        configured = await access.configured_channels("guild-1")
        allowed = await access.evaluate("guild-1", "channel-1")
        denied = await access.evaluate("guild-1", "channel-2")
        await access.remove("guild-1", ["channel-1"])
        removed = await access.evaluate("guild-1", "channel-1")
        await access.allow("guild-1", ["channel-1"])
        await access.clear("guild-1")
        cleared = await access.evaluate("guild-1", "channel-1")

        self.assertEqual(("channel-1",), configured)
        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)
        self.assertEqual("Commands are limited to: <#channel-1>", denied.detail)
        self.assertFalse(removed.allowed)
        self.assertEqual(UNCONFIGURED_MESSAGE, removed.detail)
        self.assertFalse(cleared.allowed)
        self.assertEqual(UNCONFIGURED_MESSAGE, cleared.detail)

    async def test_denies_commands_outside_a_guild(self):
        access = GuildCommandChannelAccess(InMemoryCommandChannelStore())

        decision = await access.evaluate(None, "channel-1")

        self.assertFalse(decision.allowed)
        self.assertEqual("Commands are only available in a server.", decision.detail)
