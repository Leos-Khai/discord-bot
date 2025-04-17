from discord.ext import commands
from db import add_server


class OnGuildJoin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild):
        """Event triggered when the bot joins a new server."""
        await add_server(str(guild.id))
        print(f"Joined new server: {guild.name} (ID: {guild.id})")


async def setup(bot):
    await bot.add_cog(OnGuildJoin(bot))
