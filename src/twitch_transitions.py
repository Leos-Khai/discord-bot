from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Mapping, Optional, Protocol, Sequence


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    DELIVERED = "delivered"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class TwitchSubscription:
    guild_id: str
    twitch_username: str
    notification_channel_id: str
    display_name: Optional[str] = None


@dataclass(frozen=True)
class TwitchStream:
    stream_id: str
    user_id: Optional[str]
    user_login: str
    display_name: Optional[str]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class TwitchDeliveryState:
    guild_id: str
    twitch_username: str
    is_live: bool
    stream_id: Optional[str]
    message_id: Optional[str]
    delivery_status: DeliveryStatus
    delivery_attempts: int
    user_id: Optional[str] = None
    user_login: Optional[str] = None
    display_name: Optional[str] = None


class TwitchPlatform(Protocol):
    async def live_streams(
        self, usernames: Sequence[str]
    ) -> Optional[Sequence[TwitchStream]]: ...

    async def vod_url(
        self,
        user_id: Optional[str],
        stream_id: Optional[str],
        user_login: Optional[str],
    ) -> Optional[str]: ...


class TwitchNotificationStore(Protocol):
    async def tracked_subscriptions(self) -> Sequence[TwitchSubscription]: ...

    async def delivery_state(
        self, guild_id: str, twitch_username: str
    ) -> Optional[TwitchDeliveryState]: ...

    async def save_delivery_state(self, state: TwitchDeliveryState) -> None: ...


class TwitchPublisher(Protocol):
    async def publish_live(
        self, subscription: TwitchSubscription, stream: TwitchStream
    ) -> str: ...

    async def publish_offline(
        self,
        subscription: TwitchSubscription,
        state: TwitchDeliveryState,
        vod_url: Optional[str],
    ) -> str: ...


class TwitchTransitions:
    _TWITCH_BATCH_SIZE = 100
    _MAX_DELIVERY_ATTEMPTS = 4

    def __init__(
        self,
        platform: TwitchPlatform,
        store: TwitchNotificationStore,
        publisher: TwitchPublisher,
        logger=None,
    ):
        self._platform = platform
        self._store = store
        self._publisher = publisher
        self._logger = logger
        self._unpersisted_results: dict[tuple[str, str], TwitchDeliveryState] = {}

    async def poll(self) -> None:
        subscriptions = await self._store.tracked_subscriptions()
        if not subscriptions:
            return

        streams = []
        usernames = list({subscription.twitch_username for subscription in subscriptions})
        for index in range(0, len(usernames), self._TWITCH_BATCH_SIZE):
            observed = await self._platform.live_streams(
                usernames[index : index + self._TWITCH_BATCH_SIZE]
            )
            if observed is None:
                return
            streams.extend(observed)

        streams_by_username = {
            stream.user_login.lower(): stream for stream in streams
        }
        for subscription in subscriptions:
            stream = streams_by_username.get(subscription.twitch_username.lower())
            state = await self._delivery_state(
                subscription.guild_id, subscription.twitch_username
            )
            if state and state.delivery_status is DeliveryStatus.DISPATCHING:
                await self._save_result(
                    replace(state, delivery_status=DeliveryStatus.ABANDONED)
                )
                continue
            if stream is None:
                if state and state.is_live:
                    if state.delivery_status is DeliveryStatus.DELIVERED:
                        await self._dispatch_offline(subscription, state, 1)
                    else:
                        await self._save_result(
                            replace(
                                state,
                                is_live=False,
                                delivery_status=DeliveryStatus.ABANDONED,
                            )
                        )
                elif state and state.delivery_status is DeliveryStatus.PENDING:
                    await self._dispatch_offline(
                        subscription, state, state.delivery_attempts + 1
                    )
                continue
            if state and state.is_live and state.stream_id == stream.stream_id:
                if state.delivery_status is DeliveryStatus.PENDING:
                    await self._dispatch_live(
                        subscription, stream, state.delivery_attempts + 1
                    )
                continue

            await self._dispatch_live(subscription, stream, 1)

    async def _dispatch_live(
        self,
        subscription: TwitchSubscription,
        stream: TwitchStream,
        attempt: int,
    ) -> None:
        delivery = TwitchDeliveryState(
                guild_id=subscription.guild_id,
                twitch_username=subscription.twitch_username,
                is_live=True,
                stream_id=stream.stream_id,
                message_id=None,
                delivery_status=DeliveryStatus.DISPATCHING,
                delivery_attempts=attempt,
                user_id=stream.user_id,
                user_login=stream.user_login,
                display_name=stream.display_name,
        )
        if not await self._save_intent(delivery):
            return
        try:
            message_id = await self._publisher.publish_live(subscription, stream)
        except Exception as error:
            self._log_delivery_failure(subscription, "live", attempt, error)
            await self._save_result(
                replace(
                    delivery,
                    delivery_status=(
                        DeliveryStatus.ABANDONED
                        if attempt >= self._MAX_DELIVERY_ATTEMPTS
                        else DeliveryStatus.PENDING
                    ),
                )
            )
            return
        await self._save_result(
            replace(
                delivery,
                message_id=message_id,
                delivery_status=DeliveryStatus.DELIVERED,
            )
        )

    async def _dispatch_offline(
        self,
        subscription: TwitchSubscription,
        state: TwitchDeliveryState,
        attempt: int,
    ) -> None:
        delivery = replace(
            state,
            is_live=False,
            delivery_status=DeliveryStatus.DISPATCHING,
            delivery_attempts=attempt,
        )
        if not await self._save_intent(delivery):
            return
        try:
            vod_url = await self._platform.vod_url(
                delivery.user_id, delivery.stream_id, delivery.user_login
            )
        except Exception as error:
            vod_url = None
            if self._logger:
                self._logger.warning(
                    "[Twitch] Could not resolve VOD for %s in guild %s: %s",
                    subscription.twitch_username,
                    subscription.guild_id,
                    error,
                )
        try:
            message_id = await self._publisher.publish_offline(
                subscription, delivery, vod_url
            )
        except Exception as error:
            self._log_delivery_failure(subscription, "offline", attempt, error)
            await self._save_result(
                replace(
                    delivery,
                    delivery_status=(
                        DeliveryStatus.ABANDONED
                        if attempt >= self._MAX_DELIVERY_ATTEMPTS
                        else DeliveryStatus.PENDING
                    ),
                )
            )
            return
        await self._save_result(
            replace(
                delivery,
                message_id=message_id,
                delivery_status=DeliveryStatus.DELIVERED,
            )
        )

    async def _delivery_state(
        self, guild_id: str, twitch_username: str
    ) -> Optional[TwitchDeliveryState]:
        key = (guild_id, twitch_username.lower())
        cached = self._unpersisted_results.get(key)
        if cached is not None:
            try:
                await self._store.save_delivery_state(cached)
            except Exception as error:
                self._log_storage_failure(cached, error)
            else:
                self._unpersisted_results.pop(key, None)
            return cached
        return await self._store.delivery_state(guild_id, twitch_username)

    async def _save_result(self, state: TwitchDeliveryState) -> None:
        key = (state.guild_id, state.twitch_username.lower())
        try:
            await self._store.save_delivery_state(state)
        except Exception as error:
            self._unpersisted_results[key] = state
            self._log_storage_failure(state, error)

    async def _save_intent(self, state: TwitchDeliveryState) -> bool:
        try:
            await self._store.save_delivery_state(state)
        except Exception as error:
            self._log_storage_failure(state, error)
            return False
        return True

    def _log_delivery_failure(
        self,
        subscription: TwitchSubscription,
        transition: str,
        attempt: int,
        error: Exception,
    ) -> None:
        if self._logger:
            self._logger.error(
                "[Twitch] %s delivery failed for %s in guild %s (attempt %s/%s): %s",
                transition,
                subscription.twitch_username,
                subscription.guild_id,
                attempt,
                self._MAX_DELIVERY_ATTEMPTS,
                error,
            )

    def _log_storage_failure(
        self, state: TwitchDeliveryState, error: Exception
    ) -> None:
        if self._logger:
            self._logger.error(
                "[Twitch] Could not persist %s delivery for %s in guild %s: %s",
                state.delivery_status,
                state.twitch_username,
                state.guild_id,
                error,
            )
