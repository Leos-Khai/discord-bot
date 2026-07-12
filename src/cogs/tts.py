from __future__ import annotations

import asyncio
import time
from typing import Optional

import discord
from discord.ext import commands
import edge_tts

from db import get_database_service, get_music_channels
from logger import get_logger
from tts import TtsPreferences


VOICE_CACHE_SECONDS = 24 * 60 * 60


class EdgeTtsVoiceCatalog:
    async def list_voices(self) -> list[dict]:
        return await edge_tts.list_voices()


class VoiceListView(discord.ui.View):
    PAGE_SIZE = 20

    def __init__(self, voices: list[dict]):
        super().__init__(timeout=120)
        self._voices = voices
        self._page = 0

    def _embed(self) -> discord.Embed:
        start = self._page * self.PAGE_SIZE
        page = self._voices[start : start + self.PAGE_SIZE]
        lines = [
            f"`{voice['ShortName']}` — {voice.get('Locale', 'Unknown')} ({voice.get('Gender', 'Unknown')})"
            for voice in page
        ]
        pages = max(1, (len(self._voices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        embed = discord.Embed(
            title="Available TTS voices",
            description="\n".join(lines) or "No voices match that language.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Page {self._page + 1} of {pages} • Use !setttsvoice <voice ID>")
        return embed

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        pages = max(1, (len(self._voices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._page = (self._page - 1) % pages
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        pages = max(1, (len(self._voices) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self._page = (self._page + 1) % pages
        await interaction.response.edit_message(embed=self._embed(), view=self)


class TtsCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = get_logger()
        self.preferences = TtsPreferences(get_database_service())
        self.synthesizer = EdgeTtsVoiceCatalog()
        self._voice_cache: list[dict] = []
        self._voices_cached_at = 0.0
        self._voice_lock = asyncio.Lock()

    async def cog_check(self, ctx):
        message = await self._channel_error(ctx.guild, ctx.channel.id)
        if message:
            await ctx.send(message)
            return False
        return True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        message = await self._channel_error(interaction.guild, interaction.channel_id)
        if not message:
            return True
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    async def _channel_error(
        self, guild: Optional[discord.Guild], channel_id: Optional[int]
    ) -> Optional[str]:
        if not guild:
            return "TTS commands are only available in a server."
        allowed = [int(channel) for channel in await get_music_channels(str(guild.id))]
        if not allowed:
            return "An admin must configure a music command channel before TTS can be used."
        if channel_id not in allowed:
            return "TTS commands are limited to: " + ", ".join(f"<#{channel}>" for channel in allowed)
        return None

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            return
        raise error

    async def _voices(self) -> list[dict]:
        now = time.monotonic()
        if self._voice_cache and now - self._voices_cached_at < VOICE_CACHE_SECONDS:
            return self._voice_cache
        async with self._voice_lock:
            now = time.monotonic()
            if not self._voice_cache or now - self._voices_cached_at >= VOICE_CACHE_SECONDS:
                self._voice_cache = sorted(
                    await self.synthesizer.list_voices(), key=lambda voice: voice["ShortName"].casefold()
                )
                self._voices_cached_at = now
        return self._voice_cache

    async def _ensure_voice_channel(self, ctx) -> bool:
        voice_state = getattr(ctx.author, "voice", None)
        channel = voice_state.channel if voice_state else None
        if not channel:
            await ctx.send("You are not connected to a voice channel.")
            return False
        client = ctx.guild.voice_client
        if client and client.is_connected() and client.channel != channel:
            await ctx.send("I am already being used in a different voice channel.")
            return False
        if not client:
            try:
                await self._audio(ctx.guild).ensure_connected(channel)
            except discord.DiscordException as error:
                self.logger.warning("Unable to join voice channel %s: %s", channel.id, error)
                await ctx.send(f"I could not join your voice channel: {error}")
                return False
        return True

    def _audio(self, guild: discord.Guild):
        return self.bot.guild_audio.for_guild(guild)

    @commands.hybrid_command(name="tts", help="Speak a message in your voice channel.")
    async def tts(self, ctx, *, message: str):
        if not await self._ensure_voice_channel(ctx):
            return
        guild_id, user_id = str(ctx.guild.id), str(ctx.author.id)
        voice = await self.preferences.voice_for(guild_id, user_id)
        result = await self._audio(ctx.guild).enqueue_speech(
            user_id,
            voice,
            message,
            bypass_cooldown=ctx.author.guild_permissions.administrator,
        )
        if not result.accepted:
            await ctx.send(f"TTS request rejected: {result.detail}")
        elif result.queued:
            await ctx.send("TTS message queued.")
        else:
            await ctx.send("Speaking your message.")

    @commands.hybrid_command(name="setttsvoice", help="Set your TTS voice for this server.")
    async def setttsvoice(self, ctx, *, voice: str):
        try:
            voices = await self._voices()
        except Exception as error:
            self.logger.warning("Unable to fetch Edge TTS voices: %s", error)
            await ctx.send("I could not fetch the TTS voice catalogue. Please try again later.")
            return
        matches = {item["ShortName"].casefold(): item["ShortName"] for item in voices}
        selected = matches.get(voice.casefold())
        if not selected:
            suggestions = [item["ShortName"] for item in voices if voice.casefold() in item["ShortName"].casefold()][:5]
            suffix = f" Try: {', '.join(f'`{item}`' for item in suggestions)}" if suggestions else ""
            await ctx.send(f"Unknown voice `{voice}`.{suffix}")
            return
        await self.preferences.set_voice(str(ctx.guild.id), str(ctx.author.id), selected)
        await ctx.send(f"Your TTS voice for this server is now `{selected}`.")

    @commands.hybrid_command(name="listvoice", help="List voices available for TTS.")
    async def listvoice(self, ctx, language: Optional[str] = None):
        try:
            voices = await self._voices()
        except Exception as error:
            self.logger.warning("Unable to fetch Edge TTS voices: %s", error)
            await ctx.send("I could not fetch the TTS voice catalogue. Please try again later.")
            return
        if language:
            language = language.casefold()
            voices = [
                voice
                for voice in voices
                if voice.get("Locale", "").casefold() == language
                or voice.get("Locale", "").casefold().startswith(f"{language}-")
            ]
        view = VoiceListView(voices)
        await ctx.send(embed=view._embed(), view=view)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not member.guild:
            return
        guild_id = str(member.guild.id)
        if self.bot.user and member.id == self.bot.user.id and before.channel and not after.channel:
            await self.bot.guild_audio.discard(guild_id)
            return
        client = member.guild.voice_client
        if not client or not client.channel:
            return
        if any(not channel_member.bot for channel_member in client.channel.members):
            return
        await self.bot.guild_audio.discard(guild_id)

    def cog_unload(self):
        self.bot.loop.create_task(self.bot.guild_audio.close_all())


async def setup(bot):
    await bot.add_cog(TtsCommands(bot))
