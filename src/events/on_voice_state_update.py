import discord
from discord.ext import commands
from db import get_channel_link


class OnVoiceStateUpdate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return

        # Helper function to get guild, text channel, and role
        def get_guild_entities(guild_id, text_channel_id, role_id=None):
            guild = discord.utils.get(self.bot.guilds, id=int(guild_id))
            if guild:
                text_channel = discord.utils.get(
                    guild.text_channels, id=int(text_channel_id)
                )
                role = (
                    discord.utils.get(guild.roles, id=int(role_id)) if role_id else None
                )
                return guild, text_channel, role
            return None, None, None

        # Handle transitions between channels
        if before.channel and after.channel and before.channel != after.channel:
            before_channel_id = str(before.channel.id)
            after_channel_id = str(after.channel.id)

            before_channel_link = get_channel_link(before_channel_id)
            after_channel_link = get_channel_link(after_channel_id)

            if not before_channel_link and after_channel_link:
                # Transitioning from a non-database channel to a database channel
                guild_id, text_channel_id, role_id = after_channel_link
                _, after_text_channel, role = get_guild_entities(
                    guild_id, text_channel_id, role_id
                )

                if after_text_channel:
                    message = f"{member.display_name}({member.name}) has joined {after.channel.name}."
                    if role:
                        message += f" {role.mention}"
                    await after_text_channel.send(message)

            elif before_channel_link and not after_channel_link:
                # Transitioning from a database channel to a non-database channel
                guild_id, text_channel_id, _ = before_channel_link
                _, before_text_channel, _ = get_guild_entities(
                    guild_id, text_channel_id
                )

                if before_text_channel:
                    message = f"{member.display_name}({member.name}) has left {before.channel.name}."
                    await before_text_channel.send(message)

            elif before_channel_link and after_channel_link:
                # Transitioning between database channels
                before_guild_id, before_text_channel_id, _ = before_channel_link
                after_guild_id, after_text_channel_id, role_id = after_channel_link

                _, before_text_channel, _ = get_guild_entities(
                    before_guild_id, before_text_channel_id
                )
                _, after_text_channel, role = get_guild_entities(
                    after_guild_id, after_text_channel_id, role_id
                )

                if (
                    before_text_channel
                    and after_text_channel
                    and before_text_channel == after_text_channel
                ):
                    # Same text channel for both before and after channels
                    message = (
                        f"{member.display_name}({member.name}) moved from {before.channel.name} "
                        f"to {after.channel.name}."
                    )
                    if role:
                        message += f" {role.mention}"
                    await before_text_channel.send(message)
                else:
                    # Separate text channels for before and after channels
                    if before_text_channel:
                        leave_message = f"{member.display_name}({member.name}) has left {before.channel.name}."
                        await before_text_channel.send(leave_message)

                    if after_text_channel:
                        join_message = f"{member.display_name}({member.name}) has joined {after.channel.name}."
                        if role:
                            join_message += f" {role.mention}"
                        await after_text_channel.send(join_message)

        # Handle leaving a voice channel
        elif before.channel and not after.channel:
            voice_channel_id = str(before.channel.id)
            channel_link = get_channel_link(voice_channel_id)

            if channel_link:
                guild_id, text_channel_id, _ = channel_link
                _, text_channel, _ = get_guild_entities(guild_id, text_channel_id)

                if text_channel:
                    message = f"{member.display_name}({member.name}) has left {before.channel.name}."
                    await text_channel.send(message)

        # Handle joining a voice channel
        elif not before.channel and after.channel:
            voice_channel_id = str(after.channel.id)
            channel_link = get_channel_link(voice_channel_id)

            if channel_link:
                guild_id, text_channel_id, role_id = channel_link
                _, text_channel, role = get_guild_entities(
                    guild_id, text_channel_id, role_id
                )

                if text_channel:
                    message = f"{member.display_name}({member.name}) has joined {after.channel.name}."
                    if role:
                        message += f" {role.mention}"
                    await text_channel.send(message)


async def setup(bot):
    await bot.add_cog(OnVoiceStateUpdate(bot))
