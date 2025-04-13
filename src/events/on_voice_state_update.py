import discord
from discord.ext import commands
from db import get_channel_link, get_custom_message


def replace_tokens(
    message, member, channel=None, old_channel=None, new_channel=None, role=None
):
    """Replace message tokens with actual values."""
    if not message:
        # Default messages if none is set
        if old_channel and new_channel:
            message = f"{member.display_name}({member.name}) moved from {old_channel.name} to {new_channel.name}"
        elif channel:
            if old_channel:  # Leave message
                message = (
                    f"{member.display_name}({member.name}) has left {channel.name}"
                )
            else:  # Join message
                message = (
                    f"{member.display_name}({member.name}) has joined {channel.name}"
                )

    # Append role mention if a role exists
    if role:
        message = f"{role.mention} {message}"

    replacements = {
        "$USER": member.display_name,
        "$USERNAME": member.name,
        "$NICKNAME": member.nick or member.display_name,
        "$MENTION": member.mention,
        "$CHANNEL": channel.name if channel else "",
        "$OLD_CHANNEL": old_channel.name if old_channel else "",
        "$NEW_CHANNEL": new_channel.name if new_channel else "",
    }

    for token, value in replacements.items():
        if message:
            message = message.replace(token, value)

    return message


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
                    custom_msg = get_custom_message(guild_id, "join")
                    message = (
                        replace_tokens(
                            custom_msg,
                            member,
                            channel=after.channel,
                            role=role,
                        )
                        if custom_msg
                        else f"{role.mention if role else ''} {member.display_name}({member.name}) has joined {after.channel.name}."
                    )
                    await after_text_channel.send(message)

            elif before_channel_link and not after_channel_link:
                # Transitioning from a database channel to a non-database channel
                guild_id, text_channel_id, role_id = before_channel_link
                _, before_text_channel, role = get_guild_entities(
                    guild_id, text_channel_id, role_id
                )

                if before_text_channel:
                    custom_msg = get_custom_message(guild_id, "leave")
                    message = (
                        replace_tokens(
                            custom_msg,
                            member,
                            channel=before.channel,
                            role=role,
                        )
                        if custom_msg
                        else f"{role.mention if role else ''} {member.display_name}({member.name}) has left {before.channel.name}."
                    )
                    await before_text_channel.send(message)

            elif before_channel_link and after_channel_link:
                # Transitioning between database channels
                before_guild_id, before_text_channel_id, before_role_id = (
                    before_channel_link
                )
                after_guild_id, after_text_channel_id, after_role_id = (
                    after_channel_link
                )

                _, before_text_channel, before_role = get_guild_entities(
                    before_guild_id, before_text_channel_id, before_role_id
                )
                _, after_text_channel, after_role = get_guild_entities(
                    after_guild_id, after_text_channel_id, after_role_id
                )

                if (
                    before_text_channel
                    and after_text_channel
                    and before_text_channel == after_text_channel
                ):
                    # Same text channel for both before and after channels
                    custom_msg = get_custom_message(before_guild_id, "move")
                    message = (
                        replace_tokens(
                            custom_msg,
                            member,
                            old_channel=before.channel,
                            new_channel=after.channel,
                            role=after_role,
                        )
                        if custom_msg
                        else f"{after_role.mention if after_role else ''} {member.display_name}({member.name}) moved from {before.channel.name} "
                        f"to {after.channel.name}."
                    )
                    await before_text_channel.send(message)
                else:
                    # Separate text channels for before and after channels
                    if before_text_channel:
                        custom_msg = get_custom_message(before_guild_id, "leave")
                        leave_message = (
                            replace_tokens(
                                custom_msg,
                                member,
                                channel=before.channel,
                                role=before_role,
                            )
                            if custom_msg
                            else f"{before_role.mention if before_role else ''} {member.display_name}({member.name}) has left {before.channel.name}."
                        )
                        await before_text_channel.send(leave_message)

                    if after_text_channel:
                        custom_msg = get_custom_message(after_guild_id, "join")
                        join_message = (
                            replace_tokens(
                                custom_msg,
                                member,
                                channel=after.channel,
                                role=after_role,
                            )
                            if custom_msg
                            else f"{after_role.mention if after_role else ''} {member.display_name}({member.name}) has joined {after.channel.name}."
                        )
                        await after_text_channel.send(join_message)

        # Handle leaving a voice channel
        elif before.channel and not after.channel:
            voice_channel_id = str(before.channel.id)
            channel_link = get_channel_link(voice_channel_id)

            if channel_link:
                guild_id, text_channel_id, role_id = channel_link
                _, text_channel, role = get_guild_entities(
                    guild_id, text_channel_id, role_id
                )

                if text_channel:
                    custom_msg = get_custom_message(guild_id, "leave")
                    message = (
                        replace_tokens(
                            custom_msg,
                            member,
                            channel=before.channel,
                            role=role,
                        )
                        if custom_msg
                        else f"{role.mention if role else ''} {member.display_name}({member.name}) has left {before.channel.name}."
                    )
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
                    custom_msg = get_custom_message(guild_id, "join")
                    message = (
                        replace_tokens(
                            custom_msg,
                            member,
                            channel=after.channel,
                            role=role,
                        )
                        if custom_msg
                        else f"{role.mention if role else ''} {member.display_name}({member.name}) has joined {after.channel.name}."
                    )
                    await text_channel.send(message)


async def setup(bot):
    await bot.add_cog(OnVoiceStateUpdate(bot))
