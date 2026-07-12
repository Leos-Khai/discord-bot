from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class VoiceMember:
    name: str
    display_name: str
    nickname: Optional[str]
    mention: str


@dataclass(frozen=True)
class VoiceChannel:
    id: str
    name: str


@dataclass(frozen=True)
class VoiceChannelLink:
    guild_id: str
    text_channel_id: str
    role_id: Optional[str] = None


@dataclass(frozen=True)
class VoiceAnnouncementTarget:
    channel: object
    role_mention: Optional[str] = None


@dataclass(frozen=True)
class VoiceStateTransition:
    member: VoiceMember
    before: Optional[VoiceChannel]
    after: Optional[VoiceChannel]


class VoiceAnnouncementStore(Protocol):
    async def channel_link(self, voice_channel_id: str) -> Optional[VoiceChannelLink]: ...

    async def custom_message(
        self, guild_id: str, announcement_type: str
    ) -> Optional[str]: ...


class VoiceAnnouncementPublisher(Protocol):
    async def target_for(
        self, link: VoiceChannelLink
    ) -> Optional[VoiceAnnouncementTarget]: ...

    async def publish(self, target: VoiceAnnouncementTarget, message: str) -> None: ...


class GuildVoiceAnnouncements:
    """Announces non-bot members' voice-channel transitions for one guild."""

    def __init__(self, store: VoiceAnnouncementStore, publisher: VoiceAnnouncementPublisher):
        self._store = store
        self._publisher = publisher

    async def announce(self, transition: VoiceStateTransition) -> None:
        if not transition.before and transition.after:
            await self._announce_join(transition.member, transition.after)
        elif transition.before and not transition.after:
            await self._announce_leave(transition.member, transition.before)
        elif transition.before and transition.after and transition.before != transition.after:
            await self._announce_move(
                transition.member, transition.before, transition.after
            )

    async def _announce_join(self, member: VoiceMember, channel: VoiceChannel) -> None:
        await self._announce("join", member, channel)

    async def _announce_leave(self, member: VoiceMember, channel: VoiceChannel) -> None:
        await self._announce("leave", member, channel)

    async def _announce(
        self, announcement_type: str, member: VoiceMember, channel: VoiceChannel
    ) -> None:
        destination = await self._destination(channel)
        if not destination:
            return
        link, target = destination
        await self._publish(announcement_type, member, channel, link, target)

    async def _announce_move(
        self, member: VoiceMember, before: VoiceChannel, after: VoiceChannel
    ) -> None:
        before_destination = await self._destination(before)
        after_destination = await self._destination(after)
        if (
            before_destination
            and after_destination
            and before_destination[1].channel == after_destination[1].channel
        ):
            link, target = after_destination
            await self._publish("move", member, None, link, target, before, after)
            return
        if before_destination:
            link, target = before_destination
            await self._publish("leave", member, before, link, target)
        if after_destination:
            link, target = after_destination
            await self._publish("join", member, after, link, target)

    async def _destination(
        self, channel: VoiceChannel
    ) -> Optional[tuple[VoiceChannelLink, VoiceAnnouncementTarget]]:
        link = await self._store.channel_link(channel.id)
        if not link:
            return None
        target = await self._publisher.target_for(link)
        return (link, target) if target else None

    async def _publish(
        self,
        announcement_type: str,
        member: VoiceMember,
        channel: Optional[VoiceChannel],
        link: VoiceChannelLink,
        target: VoiceAnnouncementTarget,
        old_channel: Optional[VoiceChannel] = None,
        new_channel: Optional[VoiceChannel] = None,
    ) -> None:
        template = await self._store.custom_message(link.guild_id, announcement_type)
        await self._publisher.publish(
            target,
            self._render(
                template,
                member,
                channel=channel,
                old_channel=old_channel or (channel if announcement_type == "leave" else None),
                new_channel=new_channel,
                role_mention=target.role_mention,
            ),
        )

    @staticmethod
    def _render(
        template: Optional[str],
        member: VoiceMember,
        *,
        channel: Optional[VoiceChannel] = None,
        old_channel: Optional[VoiceChannel] = None,
        new_channel: Optional[VoiceChannel] = None,
        role_mention: Optional[str] = None,
    ) -> str:
        if template:
            message = template
        elif old_channel and new_channel:
            message = f"{member.display_name}({member.name}) moved from {old_channel.name} to {new_channel.name}."
        elif channel and old_channel:
            message = f"{member.display_name}({member.name}) has left {channel.name}."
        else:
            message = f"{member.display_name}({member.name}) has joined {channel.name}."
        if role_mention:
            message = f"{role_mention} {message}"
        elif not template:
            message = f" {message}"
        replacements = {
            "$USERNAME": member.name,
            "$USER": member.display_name,
            "$NICKNAME": member.nickname or member.display_name,
            "$MENTION": member.mention,
            "$CHANNEL": channel.name if channel else "",
            "$OLD_CHANNEL": old_channel.name if old_channel else "",
            "$NEW_CHANNEL": new_channel.name if new_channel else "",
        }
        for token, value in replacements.items():
            message = message.replace(token, value)
        return message
