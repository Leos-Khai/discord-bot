import discord
from discord.ext import commands
from db import get_servers


class ExampleCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def list_servers(self, ctx):
        """List all servers stored in the database with additional details."""
        servers = get_servers()
        if not servers:
            await ctx.send("No servers found in the database.")
            return

        # Fetch guild details from the bot's current guilds
        guilds = {guild.id: guild for guild in self.bot.guilds}

        # Build the response with server details
        server_details = []
        for db_id, server_id in servers:
            guild = guilds.get(int(server_id))
            if guild:
                server_details.append(
                    f"- **{guild.name}** (ID: {server_id})\n"
                    f"  Member Count: {guild.member_count}\n"
                    f"  Owner: {guild.owner}\n"
                )
            else:
                server_details.append(f"- Server ID: {server_id} (Not Found)")

        await ctx.send("Magical Servers in the database:\n" + "\n".join(server_details))


async def setup(bot):
    await bot.add_cog(ExampleCog(bot))
