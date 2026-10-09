import base64
import os
import re
from typing import Literal, Optional

import anthropic
import discord
from discord.ext import commands
from google import genai
from google.genai import types

from cogs.admin import is_admin
from command_help import apply_parameter_descriptions, describe_parameters
from db import get_description_settings, update_description_settings
from logger import get_logger

DISCORD_MESSAGE_LIMIT = 2000
COOLDOWN_SECONDS = 10
SYSTEM_INSTRUCTION = (
    "You are a helpful assistant chatting in a Discord server. "
    "Keep replies short and conversational, and format with Discord markdown."
)
# Gemini caps a whole inline request at 20 MB and base64 grows bytes by a third.
IMAGE_BYTES_LIMIT = 14 * 1024 * 1024
# Each provider's accepted image types and the largest single image it takes, in raw bytes.
IMAGE_RULES = {
    "claude": (
        frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        # Claude caps each image at 10 MB once base64-encoded.
        7 * 1024 * 1024,
    ),
    "gemini": (
        frozenset({"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}),
        IMAGE_BYTES_LIMIT,
    ),
}
PROVIDER_NAMES = {"claude": "Claude", "gemini": "Gemini"}

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


def image_attachments(attachments, mime_types, image_limit) -> list:
    kept, total = [], 0
    for attachment in attachments:
        if (
            attachment.content_type in mime_types
            and attachment.size <= image_limit
            and total + attachment.size <= IMAGE_BYTES_LIMIT
        ):
            kept.append(attachment)
            total += attachment.size
    return kept


def split_reply(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    # ponytail: hard cut at the limit, can split mid-word or mid-code-block; break on newlines if that reads badly
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class Description(commands.Cog):
    """Answers @mentions and describes posted images with Claude or Gemini.

    `providers` maps "claude"/"gemini" to (client, model) for each configured provider.
    The first entry is the default for servers that haven't picked one.
    """

    def __init__(self, bot, providers: dict, image_prompt: Optional[str], claude_effort: str = "medium"):
        self.bot = bot
        self.providers = providers
        self.image_prompt = image_prompt or None
        self.claude_effort = claude_effort
        self.cooldowns = commands.CooldownMapping.from_cooldown(
            1, COOLDOWN_SECONDS, commands.BucketType.user
        )
        apply_parameter_descriptions(self)

    def provider_for(self, settings: dict) -> str:
        chosen = settings.get("provider")
        return chosen if chosen in self.providers else next(iter(self.providers))

    async def ask_claude(self, prompt: str, images: list) -> str:
        client, model = self.providers["claude"]
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": a.content_type,
                    "data": base64.standard_b64encode(await a.read()).decode(),
                },
            }
            for a in images
        ]
        content.append({"type": "text", "text": prompt})
        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_INSTRUCTION,
            output_config={"effort": self.claude_effort},
            messages=[{"role": "user", "content": content}],
        )
        if response.stop_reason == "refusal":
            return "Claude declined to answer that."
        return "".join(block.text for block in response.content if block.type == "text")

    async def ask_gemini(self, prompt: str, images: list) -> str:
        client, model = self.providers["gemini"]
        contents = prompt
        if images:
            contents = [
                prompt,
                *[types.Part.from_bytes(data=await a.read(), mime_type=a.content_type) for a in images],
            ]
        response = await client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION),
        )
        return response.text

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        addressed = is_addressed_to(message, self.bot.user)
        if not addressed and not (self.image_prompt and message.attachments):
            return
        settings = await get_description_settings(str(message.guild.id))
        if not settings.get("enabled"):
            return
        provider = self.provider_for(settings)
        images = image_attachments(message.attachments, *IMAGE_RULES[provider]) if self.image_prompt else []
        if not images and not addressed:
            return
        # A reply to the bot that is itself a command (e.g. "!skip") is not a question for the model.
        if (await self.bot.get_context(message)).valid:
            return

        # API keys are rate limited; react instead of replying so spam doesn't make the bot spam too.
        if self.cooldowns.update_rate_limit(message):
            await message.add_reaction("\N{HOURGLASS WITH FLOWING SAND}")
            return
        if images:
            prompt = self.image_prompt
        else:
            self_role = message.guild.self_role
            prompt = prompt_from(
                message.content, self.bot.user.id, *([self_role.id] if self_role else [])
            )
            if not prompt:
                await message.reply("Ask me something after the mention.", mention_author=False)
                return

        name = PROVIDER_NAMES[provider]
        async with message.channel.typing():
            try:
                ask = self.ask_claude if provider == "claude" else self.ask_gemini
                reply = await ask(prompt, images) or "I couldn't come up with a reply to that."
            except Exception as e:
                logger.error(f"{name} request failed in guild {message.guild.id}: {e}")
                if isinstance(e, anthropic.RateLimitError) or getattr(e, "code", None) == 429:
                    reply = f"{name} is rate limited right now. Try again in a minute."
                else:
                    reply = f"{name} couldn't answer right now. Try again later."

        for chunk in split_reply(reply):
            # Model output is untrusted text, so it must never ping @everyone, roles, or users.
            await message.reply(
                chunk, mention_author=False, allowed_mentions=discord.AllowedMentions.none()
            )

    @describe_parameters(state="on or off. Leave it out to show the current setting.")
    @commands.group(
        invoke_without_command=True,
        help="Turns AI replies to @mentions and image descriptions on or off for this server.\nUsage: !description [on|off], !description model [claude|gemini]",
    )
    @commands.guild_only()
    @is_admin()
    async def description(self, ctx, state: Optional[Literal["on", "off"]] = None):
        guild_id = str(ctx.guild.id)
        if state is not None:
            await update_description_settings(guild_id, enabled=state == "on")
        settings = await get_description_settings(guild_id)
        name = PROVIDER_NAMES[self.provider_for(settings)]
        await ctx.send(
            f"Descriptions are {'on' if settings.get('enabled') else 'off'} in this server, using {name}."
        )

    @describe_parameters(name="claude or gemini. Leave it out to show the current model.")
    @description.command(
        name="model",
        help="Picks which AI answers mentions and describes images in this server.\nUsage: !description model [claude|gemini]",
    )
    async def description_model(self, ctx, name: Optional[Literal["claude", "gemini"]] = None):
        guild_id = str(ctx.guild.id)
        if name is not None:
            if name not in self.providers:
                await ctx.send(f"{PROVIDER_NAMES[name]} isn't set up on this bot. Add its API key to .env.")
                return
            await update_description_settings(guild_id, provider=name)
        provider = self.provider_for(await get_description_settings(guild_id))
        await ctx.send(f"Descriptions use {PROVIDER_NAMES[provider]} ({self.providers[provider][1]}) in this server.")


async def setup(bot):
    providers = {}
    if os.getenv("ANTHROPIC_API_KEY"):
        providers["claude"] = (anthropic.AsyncAnthropic(), os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5")
    if os.getenv("GEMINI_API_KEY") and os.getenv("GEMINI_MODEL"):
        providers["gemini"] = (genai.Client(api_key=os.getenv("GEMINI_API_KEY")), os.getenv("GEMINI_MODEL"))
    if not providers:
        logger.info(
            "Description module not loaded: set ANTHROPIC_API_KEY, or GEMINI_API_KEY and GEMINI_MODEL, to enable it."
        )
        return
    image_prompt = os.getenv("DESCRIPTION_IMAGE_PROMPT")
    if not image_prompt:
        logger.info("Image descriptions are off: set DESCRIPTION_IMAGE_PROMPT to enable them.")
    await bot.add_cog(Description(bot, providers, image_prompt, os.getenv("CLAUDE_EFFORT") or "medium"))
