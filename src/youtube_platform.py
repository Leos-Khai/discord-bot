from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping, Optional

import aiohttp

from youtube_notification_delivery import (
    PublicationKind,
    YouTubeChannelObservation,
    YouTubePublication,
)


FetchJson = Callable[[str, Mapping[str, str]], Awaitable[dict]]


class YouTubePlatformError(RuntimeError):
    pass


class YouTubeApiAdapter:
    _BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str, *, fetch_json: Optional[FetchJson] = None, logger=None):
        self._api_key = api_key
        self._fetch_json = fetch_json or self._request_json
        self._logger = logger

    async def observe(
        self, channel_id: str, cursor: Optional[str]
    ) -> YouTubeChannelObservation:
        channel = await self._fetch_json(
            "channels", {"id": channel_id, "part": "contentDetails"}
        )
        items = channel.get("items", [])
        if not items:
            raise YouTubePlatformError(f"YouTube channel not found: {channel_id}")
        uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

        cutoff = self._parse_time(cursor) if cursor else None
        candidates: list[tuple[dict, datetime]] = []
        page_token: Optional[str] = None
        reached_history = False

        while True:
            params = {
                "playlistId": uploads_id,
                "part": "snippet,contentDetails",
                "maxResults": "50",
            }
            if page_token:
                params["pageToken"] = page_token
            page = await self._fetch_json("playlistItems", params)
            page_items = page.get("items", [])

            if cutoff is None:
                latest = max(
                    (self._playlist_added_at(item) for item in page_items),
                    default=datetime.now(timezone.utc),
                )
                return YouTubeChannelObservation((), latest.isoformat())

            for item in page_items:
                added_at = self._playlist_added_at(item)
                if added_at < cutoff:
                    reached_history = True
                    continue
                candidates.append((item, added_at))

            page_token = page.get("nextPageToken")
            if reached_history or not page_token:
                break

        metadata = await self._metadata_for(candidates)
        publications = [
            self._publication(channel_id, item, metadata.get(self._video_id(item)))
            for item, _ in candidates
        ]
        publications.sort(key=lambda publication: publication.published_at)
        next_cursor = max(
            (added_at for _, added_at in candidates), default=cutoff
        ).isoformat()
        return YouTubeChannelObservation(tuple(publications), next_cursor)

    async def _metadata_for(self, candidates: list[tuple[dict, datetime]]) -> dict[str, dict]:
        video_ids = [self._video_id(item) for item, _ in candidates]
        metadata: dict[str, dict] = {}
        try:
            for start in range(0, len(video_ids), 50):
                batch = video_ids[start : start + 50]
                if not batch:
                    continue
                response = await self._fetch_json(
                    "videos",
                    {
                        "id": ",".join(batch),
                        "part": "snippet,contentDetails,liveStreamingDetails",
                    },
                )
                metadata.update({item["id"]: item for item in response.get("items", [])})
        except Exception as error:
            if self._logger:
                self._logger.warning(
                    "YouTube publication classification failed; using Video: %s", error
                )
            return {}
        return metadata

    def _publication(
        self, channel_id: str, playlist_item: dict, video: Optional[dict]
    ) -> YouTubePublication:
        video_id = self._video_id(playlist_item)
        snippet = (video or {}).get("snippet") or playlist_item["snippet"]
        thumbnails = snippet.get("thumbnails", {})
        published_raw = (
            snippet.get("publishedAt")
            or playlist_item.get("contentDetails", {}).get("videoPublishedAt")
            or playlist_item["snippet"]["publishedAt"]
        )
        return YouTubePublication(
            publication_id=video_id,
            channel_id=channel_id,
            title=snippet.get("title", playlist_item["snippet"].get("title", video_id)),
            url=f"https://www.youtube.com/watch?v={video_id}",
            thumbnail_url=self._thumbnail_url(thumbnails),
            channel_name=snippet.get(
                "channelTitle", playlist_item["snippet"].get("channelTitle", channel_id)
            ),
            published_at=self._parse_time(published_raw),
            kind=self._publication_kind(video),
        )

    @staticmethod
    def _publication_kind(video: Optional[dict]) -> PublicationKind:
        if not video:
            return PublicationKind.VIDEO
        broadcast = video.get("snippet", {}).get("liveBroadcastContent")
        if broadcast == "live":
            return PublicationKind.LIVE
        if broadcast == "upcoming":
            return PublicationKind.UPCOMING_STREAM
        if video.get("liveStreamingDetails"):
            return PublicationKind.ARCHIVED_STREAM
        return PublicationKind.VIDEO

    @staticmethod
    def _thumbnail_url(thumbnails: dict) -> str:
        for size in ("maxres", "standard", "high", "medium", "default"):
            if thumbnails.get(size, {}).get("url"):
                return thumbnails[size]["url"]
        return ""

    @staticmethod
    def _video_id(item: dict) -> str:
        return item["snippet"]["resourceId"]["videoId"]

    @classmethod
    def _playlist_added_at(cls, item: dict) -> datetime:
        return cls._parse_time(item["snippet"]["publishedAt"])

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def _request_json(self, resource: str, params: Mapping[str, str]) -> dict:
        request_params = dict(params)
        request_params["key"] = self._api_key
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._BASE_URL}/{resource}", params=request_params
            ) as response:
                payload = await response.json()
                if response.status != 200 or "error" in payload:
                    message = payload.get("error", {}).get("message") or f"HTTP {response.status}"
                    raise YouTubePlatformError(
                        f"YouTube {resource} request failed: {message}"
                    )
                return payload
