from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence


UNCONFIGURED_MESSAGE = (
    "An administrator must configure command channels before Music or TTS can be used."
)


class CommandChannelStore(Protocol):
    async def get_command_channels(self, guild_id: str) -> Sequence[str]: ...

    async def add_command_channel(self, guild_id: str, channel_id: str) -> Sequence[str]: ...

    async def remove_command_channel(
        self, guild_id: str, channel_id: str
    ) -> Sequence[str]: ...

    async def clear_command_channels(self, guild_id: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class CommandChannelAccessDecision:
    allowed: bool
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if self.allowed == bool(self.detail):
            raise ValueError("Allowed decisions have no detail; denied decisions require it.")

    @classmethod
    def allow(cls) -> "CommandChannelAccessDecision":
        return cls(True)

    @classmethod
    def deny(cls, detail: str) -> "CommandChannelAccessDecision":
        return cls(False, detail)


class MongoCommandChannelStore:
    """Mongo adapter for Guild Command-Channel Access persistence."""

    def __init__(self, database):
        self._database = database

    async def get_command_channels(self, guild_id: str) -> Sequence[str]:
        return await self._database.get_music_channels(guild_id)

    async def add_command_channel(
        self, guild_id: str, channel_id: str
    ) -> Sequence[str]:
        return await self._database.add_music_channel(guild_id, channel_id)

    async def remove_command_channel(
        self, guild_id: str, channel_id: str
    ) -> Sequence[str]:
        return await self._database.remove_music_channel(guild_id, channel_id)

    async def clear_command_channels(self, guild_id: str) -> Sequence[str]:
        return await self._database.clear_music_channels(guild_id)


class GuildCommandChannelAccess:
    """Applies one guild's command-channel policy to Music and TTS."""

    def __init__(self, store: CommandChannelStore):
        self._store = store

    async def evaluate(
        self, guild_id: Optional[str], channel_id: Optional[str]
    ) -> CommandChannelAccessDecision:
        if not guild_id or not channel_id:
            return CommandChannelAccessDecision.deny(
                "Commands are only available in a server."
            )
        channels = await self._store.get_command_channels(guild_id)
        if not channels:
            return CommandChannelAccessDecision.deny(UNCONFIGURED_MESSAGE)
        if str(channel_id) not in channels:
            mentions = ", ".join(f"<#{channel}>" for channel in channels)
            return CommandChannelAccessDecision.deny(
                f"Commands are limited to: {mentions}"
            )
        return CommandChannelAccessDecision.allow()

    async def configured_channels(self, guild_id: str) -> tuple[str, ...]:
        return tuple(await self._store.get_command_channels(guild_id))

    async def allow(self, guild_id: str, channel_ids: Sequence[str]) -> tuple[str, ...]:
        if not channel_ids:
            return await self.configured_channels(guild_id)
        channels: Sequence[str] = ()
        for channel_id in channel_ids:
            channels = await self._store.add_command_channel(guild_id, str(channel_id))
        return tuple(channels)

    async def remove(
        self, guild_id: str, channel_ids: Sequence[str]
    ) -> tuple[str, ...]:
        if not channel_ids:
            return await self.configured_channels(guild_id)
        channels: Sequence[str] = ()
        for channel_id in channel_ids:
            channels = await self._store.remove_command_channel(guild_id, str(channel_id))
        return tuple(channels)

    async def clear(self, guild_id: str) -> tuple[str, ...]:
        return tuple(await self._store.clear_command_channels(guild_id))
