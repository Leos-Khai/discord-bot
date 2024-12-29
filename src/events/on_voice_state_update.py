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

        # Handle leaving a voice channel
        if before.channel and not after.channel:
            voice_channel_id = str(before.channel.id)
            channel_link = get_channel_link(voice_channel_id)

            if channel_link:
                guild_id, text_channel_id, _ = channel_link
                guild = discord.utils.get(self.bot.guilds, id=int(guild_id))
                text_channel = discord.utils.get(
                    guild.text_channels, id=int(text_channel_id)
                )

                if text_channel:
                    message = f"{member.display_name} has left {before.channel.name}."
                    await text_channel.send(message)

        # Handle switching voice channels in the same server
        elif before.channel and after.channel and before.channel != after.channel:
            new_channel_id = str(after.channel.id)
            new_channel_link = get_channel_link(new_channel_id)

            if new_channel_link:
                guild_id, text_channel_id, role_id = new_channel_link
                guild = discord.utils.get(self.bot.guilds, id=int(guild_id))
                text_channel = discord.utils.get(
                    guild.text_channels, id=int(text_channel_id)
                )

                if text_channel:
                    message = (
                        f"{member.display_name} moved from {before.channel.name} "
                        f"to {after.channel.name}."
                    )

                    # Ping the role if one exists
                    if role_id:
                        role = discord.utils.get(guild.roles, id=int(role_id))
                        if role:
                            message += f" {role.mention}"

                    await text_channel.send(message)

        # Handle joining a voice channel
        elif not before.channel and after.channel:
            voice_channel_id = str(after.channel.id)
            channel_link = get_channel_link(voice_channel_id)

            if channel_link:
                guild_id, text_channel_id, role_id = channel_link
                guild = discord.utils.get(self.bot.guilds, id=int(guild_id))
                text_channel = discord.utils.get(
                    guild.text_channels, id=int(text_channel_id)
                )

                if text_channel:
                    message = f"{member.display_name} has joined {after.channel.name}."

                    # Ping the role if one exists
                    if role_id:
                        role = discord.utils.get(guild.roles, id=int(role_id))
                        if role:
                            message += f" {role.mention}"

                    await text_channel.send(message)


async def setup(bot):
    await bot.add_cog(OnVoiceStateUpdate(bot))
