import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from youtube_notification_delivery import PublicationKind
from youtube_platform import YouTubeApiAdapter


def playlist_item(video_id, added_at, title):
    return {
        "id": f"playlist-{video_id}",
        "snippet": {
            "publishedAt": added_at,
            "title": title,
            "channelTitle": "Example Channel",
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
            "thumbnails": {"medium": {"url": f"https://img.example/{video_id}.jpg"}},
        },
        "contentDetails": {"videoPublishedAt": added_at},
    }


class YouTubeApiAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_paginates_to_known_history_and_classifies_a_live_publication(self):
        async def fetch_json(resource, params):
            if resource == "channels":
                return {
                    "items": [
                        {"contentDetails": {"relatedPlaylists": {"uploads": "uploads-1"}}}
                    ]
                }
            if resource == "playlistItems" and "pageToken" not in params:
                return {
                    "items": [
                        playlist_item("video-live", "2026-07-13T03:00:00Z", "Live now")
                    ],
                    "nextPageToken": "page-2",
                }
            if resource == "playlistItems":
                return {
                    "items": [
                        playlist_item("video-regular", "2026-07-13T02:00:00Z", "New video"),
                        playlist_item("video-old", "2026-07-13T00:00:00Z", "Known history"),
                    ]
                }
            if resource == "videos":
                return {
                    "items": [
                        {
                            "id": "video-live",
                            "snippet": {
                                "publishedAt": "2026-07-13T03:00:00Z",
                                "title": "Live now",
                                "channelTitle": "Example Channel",
                                "liveBroadcastContent": "live",
                                "thumbnails": {"medium": {"url": "https://img.example/live.jpg"}},
                            },
                            "contentDetails": {},
                            "liveStreamingDetails": {"actualStartTime": "2026-07-13T03:00:00Z"},
                        },
                        {
                            "id": "video-regular",
                            "snippet": {
                                "publishedAt": "2026-07-13T02:00:00Z",
                                "title": "New video",
                                "channelTitle": "Example Channel",
                                "liveBroadcastContent": "none",
                                "thumbnails": {},
                            },
                            "contentDetails": {},
                        },
                    ]
                }
            raise AssertionError(resource)

        platform = YouTubeApiAdapter("api-key", fetch_json=fetch_json)

        observation = await platform.observe(
            "channel-1", "2026-07-13T01:00:00+00:00"
        )

        self.assertEqual(
            [(item.publication_id, item.kind) for item in observation.publications],
            [
                ("video-regular", PublicationKind.VIDEO),
                ("video-live", PublicationKind.LIVE),
            ],
        )

    async def test_uses_video_when_classification_metadata_is_unavailable(self):
        async def fetch_json(resource, params):
            if resource == "channels":
                return {
                    "items": [
                        {"contentDetails": {"relatedPlaylists": {"uploads": "uploads-1"}}}
                    ]
                }
            if resource == "playlistItems":
                return {
                    "items": [
                        playlist_item("video-1", "2026-07-13T03:00:00Z", "Fallback")
                    ]
                }
            raise RuntimeError("Metadata unavailable")

        observation = await YouTubeApiAdapter(
            "api-key", fetch_json=fetch_json
        ).observe("channel-1", "2026-07-13T02:00:00+00:00")

        self.assertEqual(observation.publications[0].kind, PublicationKind.VIDEO)

    async def test_classifies_upcoming_and_archived_streams(self):
        async def fetch_json(resource, params):
            if resource == "channels":
                return {
                    "items": [
                        {"contentDetails": {"relatedPlaylists": {"uploads": "uploads-1"}}}
                    ]
                }
            if resource == "playlistItems":
                return {
                    "items": [
                        playlist_item("upcoming", "2026-07-13T04:00:00Z", "Soon"),
                        playlist_item("archived", "2026-07-13T03:00:00Z", "Replay"),
                    ]
                }
            if resource == "videos":
                return {
                    "items": [
                        {
                            "id": "upcoming",
                            "snippet": {
                                "publishedAt": "2026-07-13T04:00:00Z",
                                "title": "Soon",
                                "channelTitle": "Example Channel",
                                "liveBroadcastContent": "upcoming",
                            },
                        },
                        {
                            "id": "archived",
                            "snippet": {
                                "publishedAt": "2026-07-13T03:00:00Z",
                                "title": "Replay",
                                "channelTitle": "Example Channel",
                                "liveBroadcastContent": "none",
                            },
                            "liveStreamingDetails": {
                                "actualEndTime": "2026-07-13T03:30:00Z"
                            },
                        },
                    ]
                }
            raise AssertionError(resource)

        observation = await YouTubeApiAdapter(
            "api-key", fetch_json=fetch_json
        ).observe("channel-1", "2026-07-13T02:00:00+00:00")

        self.assertEqual(
            {item.publication_id: item.kind for item in observation.publications},
            {
                "upcoming": PublicationKind.UPCOMING_STREAM,
                "archived": PublicationKind.ARCHIVED_STREAM,
            },
        )

    async def test_first_observation_establishes_a_no_replay_baseline(self):
        async def fetch_json(resource, params):
            if resource == "channels":
                return {
                    "items": [
                        {"contentDetails": {"relatedPlaylists": {"uploads": "uploads-1"}}}
                    ]
                }
            if resource == "playlistItems":
                return {
                    "items": [
                        playlist_item("video-1", "2026-07-13T03:00:00Z", "Historical")
                    ]
                }
            raise AssertionError(resource)

        observation = await YouTubeApiAdapter(
            "api-key", fetch_json=fetch_json
        ).observe("channel-1", None)

        self.assertEqual(observation.publications, ())


if __name__ == "__main__":
    unittest.main()
