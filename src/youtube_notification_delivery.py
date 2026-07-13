from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Optional, Protocol, Sequence


class PublicationKind(StrEnum):
    LIVE = "live"
    UPCOMING_STREAM = "upcoming_stream"
    ARCHIVED_STREAM = "archived_stream"
    VIDEO = "video"


class YouTubeDeliveryStatus(StrEnum):
    PENDING = "pending"
    DISPATCHING = "dispatching"
    DELIVERED = "delivered"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class YouTubeSubscription:
    guild_id: str
    youtube_channel_id: str
    notification_channel_id: str
    subscribed_at: datetime
    channel_title: Optional[str] = None


@dataclass(frozen=True)
class YouTubePublication:
    publication_id: str
    channel_id: str
    title: str
    url: str
    thumbnail_url: str
    channel_name: str
    published_at: datetime
    kind: PublicationKind = PublicationKind.VIDEO


@dataclass(frozen=True)
class YouTubeChannelObservation:
    publications: Sequence[YouTubePublication]
    cursor: str


@dataclass(frozen=True)
class YouTubeDeliveryState:
    guild_id: str
    youtube_channel_id: str
    publication: YouTubePublication
    delivery_status: YouTubeDeliveryStatus = YouTubeDeliveryStatus.PENDING
    delivery_attempts: int = 0
    message_id: Optional[str] = None

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.guild_id, self.youtube_channel_id, self.publication.publication_id

    @property
    def is_pending(self) -> bool:
        return self.delivery_status == YouTubeDeliveryStatus.PENDING


class YouTubePlatform(Protocol):
    async def observe(
        self, channel_id: str, cursor: Optional[str]
    ) -> YouTubeChannelObservation: ...


class YouTubeNotificationStore(Protocol):
    async def tracked_subscriptions(self) -> Sequence[YouTubeSubscription]: ...

    async def observation_cursor(self, channel_id: str) -> Optional[str]: ...

    async def record_observation(
        self,
        channel_id: str,
        cursor: str,
        deliveries: Sequence[YouTubeDeliveryState],
    ) -> None: ...

    async def pending_deliveries(self) -> Sequence[YouTubeDeliveryState]: ...

    async def save_delivery(self, delivery: YouTubeDeliveryState) -> None: ...


class YouTubePublisher(Protocol):
    async def publish(
        self, subscription: YouTubeSubscription, publication: YouTubePublication
    ) -> str: ...


class YouTubeNotificationDelivery:
    _MAX_DELIVERY_ATTEMPTS = 4

    def __init__(
        self,
        platform: YouTubePlatform,
        store: YouTubeNotificationStore,
        publisher: YouTubePublisher,
        logger=None,
    ):
        self._platform = platform
        self._store = store
        self._publisher = publisher
        self._logger = logger

    async def poll(self) -> None:
        subscriptions = tuple(await self._store.tracked_subscriptions())
        by_channel: dict[str, list[YouTubeSubscription]] = {}
        for subscription in subscriptions:
            by_channel.setdefault(subscription.youtube_channel_id, []).append(subscription)

        for channel_id, channel_subscriptions in by_channel.items():
            try:
                cursor = await self._store.observation_cursor(channel_id)
                observation = await self._platform.observe(channel_id, cursor)
                deliveries = [
                    YouTubeDeliveryState(
                        guild_id=subscription.guild_id,
                        youtube_channel_id=channel_id,
                        publication=publication,
                    )
                    for publication in observation.publications
                    for subscription in channel_subscriptions
                    if publication.published_at > subscription.subscribed_at
                ]
                await self._store.record_observation(
                    channel_id, observation.cursor, deliveries
                )
            except Exception as error:
                if self._logger:
                    self._logger.error(
                        "YouTube channel observation failed for %s: %s",
                        channel_id,
                        error,
                    )

        current_subscriptions = tuple(await self._store.tracked_subscriptions())
        current = {
            (subscription.guild_id, subscription.youtube_channel_id): subscription
            for subscription in current_subscriptions
        }
        pending = sorted(
            await self._store.pending_deliveries(),
            key=lambda delivery: delivery.publication.published_at,
        )
        for delivery in pending:
            subscription = current.get(
                (delivery.guild_id, delivery.youtube_channel_id)
            )
            if subscription is None:
                continue
            dispatching = replace(
                delivery,
                delivery_status=YouTubeDeliveryStatus.DISPATCHING,
                delivery_attempts=delivery.delivery_attempts + 1,
            )
            try:
                await self._store.save_delivery(dispatching)
            except Exception as error:
                if self._logger:
                    self._logger.error(
                        "YouTube dispatch intent could not be saved for guild %s publication %s: %s",
                        delivery.guild_id,
                        delivery.publication.publication_id,
                        error,
                    )
                continue
            try:
                message_id = await self._publisher.publish(
                    subscription, delivery.publication
                )
            except Exception as error:
                failed_status = (
                    YouTubeDeliveryStatus.ABANDONED
                    if dispatching.delivery_attempts >= self._MAX_DELIVERY_ATTEMPTS
                    else YouTubeDeliveryStatus.PENDING
                )
                try:
                    await self._store.save_delivery(
                        replace(dispatching, delivery_status=failed_status)
                    )
                except Exception as storage_error:
                    if self._logger:
                        self._logger.error(
                            "YouTube failed-delivery result could not be saved for guild %s publication %s: %s",
                            delivery.guild_id,
                            delivery.publication.publication_id,
                            storage_error,
                        )
                if self._logger:
                    self._logger.warning(
                        "YouTube delivery failed for guild %s publication %s (%s/%s): %s",
                        delivery.guild_id,
                        delivery.publication.publication_id,
                        dispatching.delivery_attempts,
                        self._MAX_DELIVERY_ATTEMPTS,
                        error,
                    )
                continue
            try:
                await self._store.save_delivery(
                    replace(
                        dispatching,
                        delivery_status=YouTubeDeliveryStatus.DELIVERED,
                        message_id=message_id,
                    )
                )
            except Exception as error:
                if self._logger:
                    self._logger.error(
                        "YouTube delivery result could not be saved for guild %s publication %s: %s",
                        delivery.guild_id,
                        delivery.publication.publication_id,
                        error,
                    )
