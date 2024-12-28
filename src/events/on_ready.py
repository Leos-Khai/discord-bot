from discord.ext import commands
from db import get_servers, add_server


class OnReady(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print(f"Logged in as {self.bot.user}")

        # Retroactively check for servers
        print("Checking for missing servers in the database...")
        stored_servers = {server[1] for server in get_servers()}
        current_servers = {str(guild.id) for guild in self.bot.guilds}

        missing_servers = current_servers - stored_servers

        for server_id in missing_servers:
            add_server(server_id)
            print(f"Added missing server: {server_id}")

        print("Retroactive server check complete.")
        print("Loaded commands:")
        for command in self.bot.commands:
            print(f"- {command.name}")


async def setup(bot):
    await bot.add_cog(OnReady(bot))
