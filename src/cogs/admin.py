import discord
from discord.ext import commands
from db import (
    get_channel_links_by_guild,
    get_servers,
    add_channel_link,
    get_channel_link,
    remove_channel_link,
    update_channel_link_role,
    update_channel_link_text,
    set_custom_message,
    get_music_channels,
    add_music_channel,
    remove_music_channel,
    clear_music_channels,
)


def is_admin():
    """Check if the command invoker is an admin."""

    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator

    return commands.check(predicate)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _refresh_music_cog_limits(self, guild_id: str):
        music_cog = self.bot.get_cog("MusicCommands")
        if music_cog and hasattr(music_cog, "refresh_allowed_channels_cache"):
            await music_cog.refresh_allowed_channels_cache(guild_id)

    @commands.command(help="Lists all active channel links in the current server.")
    @is_admin()
    async def list_links(self, ctx):
        """List all channel links for the guild."""
        links = await get_channel_links_by_guild(str(ctx.guild.id))

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

    @commands.command(
        help='Links a text channel to a voice channel with an optional role.\nUsage: !link_channel #text-channel "Voice Channel Name" @role\nExample: !link_channel #general "Gaming Voice" @Members'
    )
    @is_admin()
    async def link_channel(
        self,
        ctx,
        text_channel: discord.TextChannel,
        voice_channel_name: str,
        role: discord.Role = None,
    ):
        guild_id = str(ctx.guild.id)
        text_channel_id = str(text_channel.id)

        voice_channel = discord.utils.get(
            ctx.guild.voice_channels, name=voice_channel_name
        )

        if not voice_channel:
            await ctx.send("Voice channel not found.")
            return

        voice_channel_id = str(voice_channel.id)
        role_id = str(role.id) if role else None

        try:
            await add_channel_link(guild_id, text_channel_id, voice_channel_id, role_id)

            # Set default messages for the new link
            default_messages = {
                "join": f"$USER joined $CHANNEL",
                "leave": f"$USER left $CHANNEL",
                "move": f"$USER moved from $OLD_CHANNEL to $NEW_CHANNEL",
            }

            for msg_type, msg in default_messages.items():
                await set_custom_message(guild_id, msg_type, msg)

            await ctx.send(
                f"Linked {text_channel.mention} to {voice_channel.name}{' with role ' + role.mention if role else ''}.\n"
                f"Default notification messages have been set. Use !set_message to customize them."
            )
        except ValueError as e:
            await ctx.send(str(e))

    @commands.command(
        help="Removes a channel link. Shows a list of links and lets you choose which to remove.\nUsage: !remove_channel\nThen select the number of the link to remove."
    )
    @is_admin()
    async def remove_channel(self, ctx):
        links = await get_channel_links_by_guild(str(ctx.guild.id))

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
                await remove_channel_link(link_id)
                await ctx.send("Channel link removed successfully.")
            else:
                await ctx.send("Invalid choice.")
        except discord.ext.commands.errors.CommandInvokeError as e:
            if isinstance(e.original, discord.errors.TimeoutError):
                await ctx.send("You took too long to respond.")

    @commands.command(
        help='Updates the text channel for an existing voice channel link.\nUsage: !update_channel "Voice Channel Name" #new-text-channel\nExample: !update_channel "Gaming Voice" #gaming-chat'
    )
    @is_admin()
    async def update_channel(
        self, ctx, voice_channel_name: str, new_text_channel: discord.TextChannel
    ):
        voice_channel = discord.utils.get(
            ctx.guild.voice_channels, name=voice_channel_name
        )

        if not voice_channel:
            await ctx.send("Voice channel not found.")
            return

        voice_channel_id = str(voice_channel.id)
        new_text_channel_id = str(new_text_channel.id)

        if await update_channel_link_text(voice_channel_id, new_text_channel_id):
            await ctx.send(
                f"Updated {voice_channel.name} to use text channel {new_text_channel.mention}."
            )
        else:
            await ctx.send("No link found for the specified voice channel.")

    @commands.command(
        help='Updates the role for an existing voice channel link.\nUsage: !update_role "Voice Channel Name" @new-role\nExample: !update_role "Gaming Voice" @Gamers'
    )
    @is_admin()
    async def update_role(
        self, ctx, voice_channel_name: str, new_role: discord.Role = None
    ):
        voice_channel = discord.utils.get(
            ctx.guild.voice_channels, name=voice_channel_name
        )

        if not voice_channel:
            await ctx.send("Voice channel not found.")
            return

        voice_channel_id = str(voice_channel.id)
        new_role_id = str(new_role.id) if new_role else None

        if await update_channel_link_role(voice_channel_id, new_role_id):
            await ctx.send(
                f"Updated {voice_channel.name} to use role {new_role.mention if new_role else 'None'}."
            )
        else:
            await ctx.send("No link found for the specified voice channel.")

    @commands.command(
        help='Removes the role from an existing voice channel link.\nUsage: !remove_role "Voice Channel Name"\nExample: !remove_role "Gaming Voice"'
    )
    @is_admin()
    async def remove_role(self, ctx, voice_channel_name: str):
        voice_channel = discord.utils.get(
            ctx.guild.voice_channels, name=voice_channel_name
        )

        if not voice_channel:
            await ctx.send("Voice channel not found.")
            return

        voice_channel_id = str(voice_channel.id)

        if await update_channel_link_role(voice_channel_id, None):
            await ctx.send(f"Removed role from {voice_channel.name}.")
        else:
            await ctx.send("No link found for the specified voice channel.")

    @commands.command(
        help="""Sets a custom notification message for voice channel events.
Available message types: join, leave, move
Available tokens:
$USER - Member's display name
$USERNAME - Member's username
$NICKNAME - Member's server nickname
$MENTION - Mentions the user
$CHANNEL - Current voice channel name
$OLD_CHANNEL - Previous voice channel name (for move events)
$NEW_CHANNEL - New voice channel name (for move events)

Example: !set_message join "$USER joined $CHANNEL"
Example: !set_message move "$USER moved from $OLD_CHANNEL to $NEW_CHANNEL"
Example: !set_message leave "$MENTION left $CHANNEL"
Example: !set_message reset join (Resets join message to default)
Example: !set_message reset all (Resets all messages to default)
        """
    )
    @is_admin()
    async def set_message(self, ctx, msg_type: str, *, message: str = None):
        msg_type = msg_type.lower()

        if msg_type == "reset":
            if not message or message.lower() not in ["join", "leave", "move", "all"]:
                await ctx.send(
                    "Please specify what to reset: 'join', 'leave', 'move', or 'all'"
                )
                return

            message = message.lower()
            if message == "all":
                # Reset all message types
                for type_to_reset in ["join", "leave", "move"]:
                    await set_custom_message(str(ctx.guild.id), type_to_reset, None)
                await ctx.send("All message types have been reset to default.")
            else:
                # Reset specific message type
                await set_custom_message(str(ctx.guild.id), message, None)
                await ctx.send(
                    f"Message for {message} events has been reset to default."
                )
            return

        if msg_type not in ["join", "leave", "move"]:
            await ctx.send("Message type must be 'join', 'leave', or 'move'")
            return

        try:
            await set_custom_message(str(ctx.guild.id), msg_type, message)
            await ctx.send(f"Successfully set custom message for {msg_type} events.")
        except ValueError as e:
            await ctx.send(str(e))

    @commands.group(
        name="music_channels",
        invoke_without_command=True,
        help="List or manage which text channels can run music commands.\nUsage: !music_channels (lists), !music_channels add #channel, !music_channels remove #channel, !music_channels clear",
    )
    @is_admin()
    async def music_channels(self, ctx):
        guild_id = str(ctx.guild.id)
        allowed = await get_music_channels(guild_id)
        if not allowed:
            await ctx.send("No limits set. Music commands can run in any channel.")
            return
        mentions = ", ".join(f"<#{cid}>" for cid in allowed)
        await ctx.send(f"Music commands are limited to: {mentions}")

    @music_channels.command(
        name="add",
        help="Allow one or more text channels to run music commands.\nUsage: !music_channels add #music [#dj ...]",
    )
    async def music_channels_add(self, ctx, *channels: discord.TextChannel):
        if not channels:
            await ctx.send("Please mention at least one text channel to allow.")
            return

        guild_id = str(ctx.guild.id)
        for channel in channels:
            await add_music_channel(guild_id, str(channel.id))

        await self._refresh_music_cog_limits(guild_id)
        updated = await get_music_channels(guild_id)
        mentions = ", ".join(f"<#{cid}>" for cid in updated)
        await ctx.send(f"Updated allowed channels: {mentions}")

    @music_channels.command(
        name="remove",
        help="Remove one or more text channels from the allowed list.\nUsage: !music_channels remove #music [#dj ...]",
    )
    async def music_channels_remove(self, ctx, *channels: discord.TextChannel):
        if not channels:
            await ctx.send("Please mention at least one text channel to remove.")
            return

        guild_id = str(ctx.guild.id)
        for channel in channels:
            await remove_music_channel(guild_id, str(channel.id))

        await self._refresh_music_cog_limits(guild_id)
        updated = await get_music_channels(guild_id)
        if not updated:
            await ctx.send(
                "Removed those channels. No limits remain; music commands can run anywhere."
            )
            return
        mentions = ", ".join(f"<#{cid}>" for cid in updated)
        await ctx.send(f"Updated allowed channels: {mentions}")

    @music_channels.command(
        name="clear",
        help="Remove all channel limits for music commands.\nUsage: !music_channels clear",
    )
    async def music_channels_clear(self, ctx):
        guild_id = str(ctx.guild.id)
        await clear_music_channels(guild_id)
        await self._refresh_music_cog_limits(guild_id)
        await ctx.send("Cleared limits. Music commands can run in any channel.")

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.send("You do not have permission to use this command.")
            return
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Admin(bot))
