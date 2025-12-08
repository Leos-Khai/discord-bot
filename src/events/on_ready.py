from discord.ext import commands
from db import get_servers, add_server
from logger import get_logger


class OnReady(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.logger = get_logger()
        self.synced = False

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"Logged in as {self.bot.user}")

        # Retroactively check for servers
        print("Checking for missing servers in the database...")
        stored_servers = {server["server_id"] for server in await get_servers()}
        current_servers = {str(guild.id) for guild in self.bot.guilds}

        missing_servers = current_servers - stored_servers

        for server_id in missing_servers:
            await add_server(server_id)
            print(f"Added missing server: {server_id}")

        print("Retroactive server check complete.")
        print("Loaded commands:")
        for command in self.bot.commands:
            print(f"- {command.name}")

        if not self.synced:
            try:
                synced = await self.bot.tree.sync()
                self.logger.info(f"Synced {len(synced)} slash commands.")
                self.synced = True
            except Exception as e:
                self.logger.error(f"Failed to sync slash commands: {e}")


async def setup(bot):
    await bot.add_cog(OnReady(bot))
