import discord
from discord.ext import commands
from db import (
    get_channel_links_by_guild,
    get_servers,
    add_channel_link,
    get_channel_link,
    remove_channel_link,
)


def is_admin():
    """Check if the command invoker is an admin."""

    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator

    return commands.check(predicate)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @is_admin()
    async def link_channel(
        self,
        ctx,
        text_channel: discord.TextChannel,
        voice_channel_name: str,
        role: discord.Role = None,
    ):
        """Link a text channel, voice channel, and optional role."""

        guild_id = str(ctx.guild.id)

        text_channel_id = str(text_channel.id)

        # Search for the voice channel by name
        voice_channel = discord.utils.get(
            ctx.guild.voice_channels, name=voice_channel_name
        )

        if not voice_channel:
            await ctx.send("Voice channel not found.")
            return

        voice_channel_id = str(voice_channel.id)
        role_id = str(role.id) if role else None

        try:
            add_channel_link(guild_id, text_channel_id, voice_channel_id, role_id)
            await ctx.send(
                f"Linked {text_channel.mention} to {voice_channel.name}{' with role ' + role.mention if role else ''}."
            )
        except ValueError as e:
            await ctx.send(str(e))

    @commands.command()
    @is_admin()
    async def remove_channel(self, ctx):
        """List and remove a channel link."""
        links = get_channel_links_by_guild(str(ctx.guild.id))

        if not links:
            await ctx.send("No channel links found.")
            return

        description = "Here are the current links:\n"
        for idx, (link_id, text_id, voice_id, role_id) in enumerate(links, start=1):
            text_channel = discord.utils.get(ctx.guild.text_channels, id=int(text_id))
            voice_channel = discord.utils.get(
                ctx.guild.voice_channels, id=int(voice_id)
            )
            role = (
                discord.utils.get(ctx.guild.roles, id=int(role_id)) if role_id else None
            )
            description += (
                f"{idx}: Text: {text_channel.mention if text_channel else text_id}, "
                f"Voice: {voice_channel.name if voice_channel else voice_id}, "
                f"Role: {role.name if role else 'None'}\n"
            )

        await ctx.send(description)

        def check(msg):
            return (
                msg.author == ctx.author
                and msg.channel == ctx.channel
                and msg.content.isdigit()
            )

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=30)
            choice = int(msg.content) - 1
            if 0 <= choice < len(links):
                link_id = links[choice][0]
                remove_channel_link(link_id)
                await ctx.send("Channel link removed successfully.")
            else:
                await ctx.send("Invalid choice.")
        except discord.ext.commands.errors.CommandInvokeError as e:
            if isinstance(e.original, discord.errors.TimeoutError):
                await ctx.send("You took too long to respond.")

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

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        """Handle errors for the cog."""
        if isinstance(error, commands.CheckFailure):
            await ctx.send("You do not have permission to use this command.")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Admin(bot))
