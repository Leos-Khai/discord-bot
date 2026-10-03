import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cogs.gemini import Gemini, is_addressed_to, prompt_from, split_reply
from db import DatabaseService

BOT = SimpleNamespace(id=1, bot=True)
ROLE = SimpleNamespace(id=2)


def make_message(content="hi", *, author_bot=False, guild=True, mentions=(), role_mentions=(), replied_to=None):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=9, bot=author_bot),
        guild=SimpleNamespace(id=5, self_role=ROLE) if guild else None,
        mentions=list(mentions),
        role_mentions=list(role_mentions),
        reference=SimpleNamespace(resolved=replied_to) if replied_to else None,
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
        channel=SimpleNamespace(typing=MagicMock()),
    )


class TriggerTests(unittest.TestCase):
    def test_responds_to_mentions_and_replies_to_the_bot(self):
        self.assertTrue(is_addressed_to(make_message(mentions=[BOT]), BOT))
        self.assertTrue(is_addressed_to(make_message(role_mentions=[ROLE]), BOT))
        self.assertTrue(is_addressed_to(make_message(replied_to=SimpleNamespace(author=BOT)), BOT))

    def test_ignores_bots_dms_and_unaddressed_messages(self):
        self.assertFalse(is_addressed_to(make_message(mentions=[BOT], author_bot=True), BOT))
        self.assertFalse(is_addressed_to(make_message(mentions=[BOT], guild=False), BOT))
        self.assertFalse(is_addressed_to(make_message(), BOT))
        self.assertFalse(is_addressed_to(make_message(replied_to=SimpleNamespace(author=SimpleNamespace(id=7))), BOT))

    def test_strips_bot_mentions_from_the_prompt(self):
        self.assertEqual("what is <@7> up to?", prompt_from("<@1> what is <@7> up to? <@!1><@&2>", 1, 2))

    def test_splits_replies_at_the_discord_limit(self):
        text = "a" * 4500
        chunks = split_reply(text)
        self.assertEqual([2000, 2000, 500], [len(chunk) for chunk in chunks])
        self.assertEqual(text, "".join(chunks))
        self.assertEqual(["short"], split_reply("short"))


class FakeSettingsCollection:
    def __init__(self):
        self.docs = {}

    async def find_one(self, query, projection=None):
        return self.docs.get(query["guild_id"])

    async def update_one(self, query, update, *, upsert=False):
        self.docs.setdefault(query["guild_id"], {}).update(update["$set"])


class ToggleStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_off_by_default_and_remembers_each_guilds_choice(self):
        database = object.__new__(DatabaseService)
        database.guild_gemini_settings = FakeSettingsCollection()

        self.assertFalse(await database.get_gemini_enabled("guild-1"))
        await database.set_gemini_enabled("guild-1", True)
        self.assertTrue(await database.get_gemini_enabled("guild-1"))
        self.assertFalse(await database.get_gemini_enabled("guild-2"))
        await database.set_gemini_enabled("guild-1", False)
        self.assertFalse(await database.get_gemini_enabled("guild-1"))


def make_cog(reply_text="hello"):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=SimpleNamespace(text=reply_text))
    bot = SimpleNamespace(user=BOT, get_context=AsyncMock(return_value=SimpleNamespace(valid=False)))
    return Gemini(bot, client, "test-model"), client.aio.models.generate_content


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stays_silent_when_the_guild_has_it_off(self):
        cog, generate = make_cog("hello")
        message = make_message("<@1> hi", mentions=[BOT])

        with patch("cogs.gemini.get_gemini_enabled", AsyncMock(return_value=False)):
            await cog.on_message(message)

        generate.assert_not_called()
        message.reply.assert_not_called()

    async def test_replies_in_chunks_without_pinging_anyone(self):
        cog, generate = make_cog("x" * 2500)
        message = make_message("<@1> tell me a story", mentions=[BOT])

        with patch("cogs.gemini.get_gemini_enabled", AsyncMock(return_value=True)):
            await cog.on_message(message)

        self.assertEqual("tell me a story", generate.call_args.kwargs["contents"])
        self.assertEqual("test-model", generate.call_args.kwargs["model"])
        self.assertEqual([2000, 500], [len(call.args[0]) for call in message.reply.call_args_list])
        for call in message.reply.call_args_list:
            self.assertEqual(discord.AllowedMentions.none().to_dict(), call.kwargs["allowed_mentions"].to_dict())

    async def test_rate_limits_each_user(self):
        cog, generate = make_cog("hello")

        with patch("cogs.gemini.get_gemini_enabled", AsyncMock(return_value=True)):
            await cog.on_message(make_message("<@1> one", mentions=[BOT]))
            second = make_message("<@1> two", mentions=[BOT])
            await cog.on_message(second)

        self.assertEqual(1, generate.call_count)
        second.add_reaction.assert_called_once()


class AdminGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_command_requires_administrator(self):
        cog, _ = make_cog()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=5),
            author=SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True)),
        )

        results = [await discord.utils.maybe_coroutine(check, ctx) for check in cog.gemini.checks]

        self.assertIn(False, results)


if __name__ == "__main__":
    unittest.main()
