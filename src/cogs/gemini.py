import os
import re
from typing import Literal, Optional

import discord
from discord.ext import commands
from google import genai
from google.genai import types

from cogs.admin import is_admin
from command_help import apply_parameter_descriptions, describe_parameters
from db import get_gemini_enabled, set_gemini_enabled
from logger import get_logger

DISCORD_MESSAGE_LIMIT = 2000
COOLDOWN_SECONDS = 10
SYSTEM_INSTRUCTION = (
    "You are a helpful assistant chatting in a Discord server. "
    "Keep replies short and conversational, and format with Discord markdown."
)
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"})
# Gemini caps a whole inline request at 20 MB and base64 grows bytes by a third.
IMAGE_BYTES_LIMIT = 14 * 1024 * 1024

logger = get_logger()


def is_addressed_to(message, bot_user) -> bool:
    """A server message from a person that mentions the bot or replies to it."""
    if message.author.bot or message.guild is None:
        return False
    if bot_user in message.mentions:
        return True
    # Discord's mention autocomplete sometimes inserts the bot's integration role instead of the bot user.
    if message.guild.self_role and message.guild.self_role in message.role_mentions:
        return True
    replied = message.reference.resolved if message.reference else None
    return getattr(replied, "author", None) == bot_user


def prompt_from(content: str, *mention_ids: int) -> str:
    pattern = "|".join(rf"<@[!&]?{mention_id}>" for mention_id in mention_ids)
    return re.sub(pattern, "", content).strip()


def image_attachments(attachments) -> list:
    kept, total = [], 0
    for attachment in attachments:
        if attachment.content_type in IMAGE_MIME_TYPES and total + attachment.size <= IMAGE_BYTES_LIMIT:
            kept.append(attachment)
            total += attachment.size
    return kept


def split_reply(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    # ponytail: hard cut at the limit, can split mid-word or mid-code-block; break on newlines if that reads badly
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class Gemini(commands.Cog):
    def __init__(self, bot, client: genai.Client, model: str, image_prompt: Optional[str]):
        self.bot = bot
        self.client = client
        self.model = model
        self.image_prompt = image_prompt or None
        self.cooldowns = commands.CooldownMapping.from_cooldown(
            1, COOLDOWN_SECONDS, commands.BucketType.user
        )
        apply_parameter_descriptions(self)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        images = image_attachments(message.attachments) if self.image_prompt else []
        if not images and not is_addressed_to(message, self.bot.user):
            return
        if not await get_gemini_enabled(str(message.guild.id)):
            return
        # A reply to the bot that is itself a command (e.g. "!skip") is not a question for Gemini.
        if (await self.bot.get_context(message)).valid:
            return

        # Free-tier Gemini keys are rate limited; react instead of replying so spam doesn't make the bot spam too.
        if self.cooldowns.update_rate_limit(message):
            await message.add_reaction("\N{HOURGLASS WITH FLOWING SAND}")
            return
        prompt = None
        if not images:
            self_role = message.guild.self_role
            prompt = prompt_from(
                message.content, self.bot.user.id, *([self_role.id] if self_role else [])
            )
            if not prompt:
                await message.reply("Ask me something after the mention.", mention_author=False)
                return

        async with message.channel.typing():
            try:
                if images:
                    prompt = [
                        self.image_prompt,
                        *[
                            types.Part.from_bytes(data=await a.read(), mime_type=a.content_type)
                            for a in images
                        ],
                    ]
                response = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION),
                )
                reply = response.text or "I couldn't come up with a reply to that."
            except Exception as e:
                logger.error(f"Gemini request failed in guild {message.guild.id}: {e}")
                if getattr(e, "code", None) == 429:
                    reply = "Gemini is rate limited right now. Try again in a minute."
                else:
                    reply = "Gemini couldn't answer right now. Try again later."

        for chunk in split_reply(reply):
            # Model output is untrusted text, so it must never ping @everyone, roles, or users.
            await message.reply(
                chunk, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )

    @describe_parameters(state="on or off. Leave it out to show the current setting.")
    @commands.command(
        help="Turns Gemini replies to @mentions and image descriptions on or off for this server.\nUsage: !gemini [on|off]"
    )
    @commands.guild_only()
    @is_admin()
    async def gemini(self, ctx, state: Optional[Literal["on", "off"]] = None):
        guild_id = str(ctx.guild.id)
        if state is not None:
            await set_gemini_enabled(guild_id, state == "on")
        enabled = await get_gemini_enabled(guild_id)
        await ctx.send(f"Gemini replies are {'on' if enabled else 'off'} in this server.")


async def setup(bot):
    api_key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL")
    if not api_key or not model:
        logger.info("Gemini module not loaded: set GEMINI_API_KEY and GEMINI_MODEL to enable it.")
        return
    image_prompt = os.getenv("GEMINI_IMAGE_PROMPT")
    if not image_prompt:
        logger.info("Gemini image descriptions are off: set GEMINI_IMAGE_PROMPT to enable them.")
    await bot.add_cog(Gemini(bot, genai.Client(api_key=api_key), model, image_prompt))
