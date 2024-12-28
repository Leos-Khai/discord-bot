import discord
from discord.ext import commands


class OnVoiceStateUpdate(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel is None and after.channel is not None:
            print(f"{member} joined {after.channel.name}")
        elif before.channel is not None and after.channel is None:
            print(f"{member} left {before.channel.name}")
        elif before.channel is not None and after.channel is not None:
            print(f"{member} moved from {before.channel.name} to {after.channel.name}")


async def setup(bot):
    await bot.add_cog(OnVoiceStateUpdate(bot))
