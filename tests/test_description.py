import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cogs.description import IMAGE_RULES, Description, image_attachments, is_addressed_to, prompt_from, split_reply
from db import DatabaseService

BOT = SimpleNamespace(id=1, bot=True)
ROLE = SimpleNamespace(id=2)


def make_attachment(content_type="image/png", size=3, data=b"png"):
    return SimpleNamespace(content_type=content_type, size=size, read=AsyncMock(return_value=data))


def make_message(
    content="hi", *, author_bot=False, guild=True, mentions=(), role_mentions=(), replied_to=None, attachments=()
):
    return SimpleNamespace(
        content=content,
        attachments=list(attachments),
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

    def test_keeps_supported_images_within_the_size_budget(self):
        gif = make_attachment("image/gif")
        untyped = make_attachment(None)
        png = make_attachment("image/png", size=10 * 1024 * 1024)
        jpeg = make_attachment("image/jpeg", size=2 * 1024 * 1024)
        too_big = make_attachment("image/webp", size=3 * 1024 * 1024)
        small = make_attachment("image/webp", size=1024 * 1024)

        self.assertEqual(
            [png, jpeg, small],
            image_attachments([gif, png, untyped, jpeg, too_big, small], *IMAGE_RULES["gemini"]),
        )

    def test_claude_takes_gifs_but_not_heic_or_images_over_its_per_image_cap(self):
        gif = make_attachment("image/gif")
        heic = make_attachment("image/heic")
        too_big = make_attachment("image/png", size=8 * 1024 * 1024)

        self.assertEqual([gif], image_attachments([gif, heic, too_big], *IMAGE_RULES["claude"]))


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

        self.assertFalse((await database.get_description_settings("guild-1")).get("enabled"))
        await database.update_description_settings("guild-1", enabled=True)
        await database.update_description_settings("guild-1", provider="gemini")
        settings = await database.get_description_settings("guild-1")
        self.assertTrue(settings["enabled"])
        self.assertEqual("gemini", settings["provider"])
        self.assertEqual({}, await database.get_description_settings("guild-2"))
        await database.update_description_settings("guild-1", enabled=False)
        self.assertFalse((await database.get_description_settings("guild-1"))["enabled"])


def make_bot():
    return SimpleNamespace(user=BOT, get_context=AsyncMock(return_value=SimpleNamespace(valid=False)))


def make_cog(reply_text="hello", image_prompt=None):
    client = MagicMock()
    client.aio.models.generate_content = AsyncMock(return_value=SimpleNamespace(text=reply_text))
    cog = Description(make_bot(), {"gemini": (client, "test-model")}, image_prompt)
    return cog, client.aio.models.generate_content


def make_claude_cog(image_prompt=None):
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="hello")],
        )
    )
    cog = Description(make_bot(), {"claude": (client, "claude-haiku-5-5")}, image_prompt)
    return cog, client.messages.create


def enabled(**settings):
    return patch("cogs.description.get_description_settings", AsyncMock(return_value={"enabled": True, **settings}))


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stays_silent_when_the_guild_has_it_off(self):
        cog, generate = make_cog("hello")
        message = make_message("<@1> hi", mentions=[BOT])

        with patch("cogs.description.get_description_settings", AsyncMock(return_value={})):
            await cog.on_message(message)

        generate.assert_not_called()
        message.reply.assert_not_called()

    async def test_replies_in_chunks_without_pinging_anyone(self):
        cog, generate = make_cog("x" * 2500)
        message = make_message("<@1> tell me a story", mentions=[BOT])

        with enabled():
            await cog.on_message(message)

        self.assertEqual("tell me a story", generate.call_args.kwargs["contents"])
        self.assertEqual("test-model", generate.call_args.kwargs["model"])
        self.assertEqual([2000, 500], [len(call.args[0]) for call in message.reply.call_args_list])
        for call in message.reply.call_args_list:
            self.assertEqual(discord.AllowedMentions.none().to_dict(), call.kwargs["allowed_mentions"].to_dict())

    async def test_describes_images_posted_without_a_mention(self):
        cog, generate = make_cog("A cat.", image_prompt="Describe it.")
        message = make_message("look", attachments=[make_attachment()])

        with enabled():
            await cog.on_message(message)

        generate.assert_called_once()
        contents = generate.call_args.kwargs["contents"]
        self.assertEqual("Describe it.", contents[0])
        self.assertEqual(b"png", contents[1].inline_data.data)
        self.assertEqual("image/png", contents[1].inline_data.mime_type)
        self.assertEqual("A cat.", message.reply.call_args.args[0])

    async def test_ignores_images_when_no_image_prompt_is_set(self):
        cog, generate = make_cog("hello", image_prompt=None)
        image_only = make_message("look", attachments=[make_attachment()])
        mention = make_message("<@1> hi", mentions=[BOT])

        with enabled():
            await cog.on_message(image_only)
            generate.assert_not_called()
            await cog.on_message(mention)

        generate.assert_called_once()
        self.assertEqual("hi", generate.call_args.kwargs["contents"])

    async def test_rate_limits_each_user(self):
        cog, generate = make_cog("hello")

        with enabled():
            await cog.on_message(make_message("<@1> one", mentions=[BOT]))
            second = make_message("<@1> two", mentions=[BOT])
            await cog.on_message(second)

        self.assertEqual(1, generate.call_count)
        second.add_reaction.assert_called_once()


class ClaudeTests(unittest.IsolatedAsyncioTestCase):
    async def test_asks_claude_at_medium_effort_and_replies_with_its_text(self):
        cog, create = make_claude_cog()
        message = make_message("<@1> hi", mentions=[BOT])

        with enabled():
            await cog.on_message(message)

        kwargs = create.call_args.kwargs
        self.assertEqual("claude-haiku-5-5", kwargs["model"])
        self.assertEqual({"effort": "medium"}, kwargs["output_config"])
        self.assertEqual([{"type": "text", "text": "hi"}], kwargs["messages"][0]["content"])
        self.assertEqual("hello", message.reply.call_args.args[0])

    async def test_sends_images_to_claude_as_base64_before_the_prompt(self):
        cog, create = make_claude_cog(image_prompt="Describe it.")
        message = make_message("look", attachments=[make_attachment()])

        with enabled():
            await cog.on_message(message)

        image, text = create.call_args.kwargs["messages"][0]["content"]
        self.assertEqual({"type": "base64", "media_type": "image/png", "data": "cG5n"}, image["source"])
        self.assertEqual({"type": "text", "text": "Describe it."}, text)

    async def test_uses_the_guilds_chosen_provider(self):
        gemini_cog, generate = make_cog("from gemini")
        claude_cog, create = make_claude_cog()
        cog = Description(
            make_bot(), {**claude_cog.providers, **gemini_cog.providers}, None
        )
        message = make_message("<@1> hi", mentions=[BOT])

        with enabled(provider="gemini"):
            await cog.on_message(message)

        create.assert_not_called()
        generate.assert_called_once()
        self.assertEqual("from gemini", message.reply.call_args.args[0])


class ModelCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_switches_only_to_configured_providers(self):
        cog, _ = make_claude_cog()
        ctx = SimpleNamespace(guild=SimpleNamespace(id=5), send=AsyncMock())
        update = AsyncMock()

        with patch("cogs.description.update_description_settings", update), enabled(provider="claude"):
            await cog.description_model.callback(cog, ctx, "gemini")
            update.assert_not_called()
            self.assertIn("isn't set up", ctx.send.call_args.args[0])

            await cog.description_model.callback(cog, ctx, "claude")
            update.assert_called_once_with("5", provider="claude")
            self.assertEqual("Descriptions use Claude (claude-haiku-5-5) in this server.", ctx.send.call_args.args[0])


class AdminGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_toggle_command_requires_administrator(self):
        cog, _ = make_cog()
        ctx = SimpleNamespace(
            guild=SimpleNamespace(id=5),
            author=SimpleNamespace(guild_permissions=discord.Permissions(manage_guild=True)),
        )

        results = [await discord.utils.maybe_coroutine(check, ctx) for check in cog.description.checks]

        self.assertIn(False, results)


if __name__ == "__main__":
    unittest.main()
