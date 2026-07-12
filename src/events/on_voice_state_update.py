import discord
from discord.ext import commands

from db import get_database_service
from guild_voice_announcements import (
    GuildVoiceAnnouncements,
    VoiceAnnouncementTarget,
    VoiceChannel,
    VoiceChannelLink,
    VoiceMember,
    VoiceStateTransition,
)


class MongoVoiceAnnouncementStore:
    """Database adapter for Guild Voice Announcements."""

    def __init__(self, database):
        self._database = database

    async def channel_link(self, voice_channel_id: str):
        link = await self._database.get_channel_link(voice_channel_id)
        if not link:
            return None
        return VoiceChannelLink(*link)

    async def custom_message(self, guild_id: str, announcement_type: str):
        return await self._database.get_custom_message(guild_id, announcement_type)


class DiscordVoiceAnnouncementPublisher:
    """Discord adapter for Guild Voice Announcements."""

    def __init__(self, bot):
        self._bot = bot

    async def target_for(self, link: VoiceChannelLink):
        guild = discord.utils.get(self._bot.guilds, id=int(link.guild_id))
        if not guild:
            return None
        text_channel = discord.utils.get(guild.text_channels, id=int(link.text_channel_id))
        if not text_channel:
            return None
        role = discord.utils.get(guild.roles, id=int(link.role_id)) if link.role_id else None
        return VoiceAnnouncementTarget(text_channel, role.mention if role else None)

    async def publish(self, target, message: str) -> None:
        await target.channel.send(message)


class OnVoiceStateUpdate(commands.Cog):
    def __init__(self, bot):
        self.announcements = GuildVoiceAnnouncements(
            MongoVoiceAnnouncementStore(get_database_service()),
            DiscordVoiceAnnouncementPublisher(bot),
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        await self.announcements.announce(
            VoiceStateTransition(
                VoiceMember(
                    member.name,
                    member.display_name,
                    member.nick,
                    member.mention,
                ),
                self._channel(before.channel),
                self._channel(after.channel),
            )
        )

    @staticmethod
    def _channel(channel):
        return VoiceChannel(str(channel.id), channel.name) if channel else None


async def setup(bot):
    await bot.add_cog(OnVoiceStateUpdate(bot))
