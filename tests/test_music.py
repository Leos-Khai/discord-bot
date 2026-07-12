import asyncio
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cogs.music import YtdlpMediaAdapter
from guild_playback import TrackRequest


class YtdlpMediaAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_a_text_request_to_the_first_youtube_search_result(self):
        adapter = YtdlpMediaAdapter(logger=None)
        calls = []

        async def extract(target, **kwargs):
            calls.append(target)
            if target == "ytsearch1:song name":
                return {"entries": [{"webpage_url": "https://youtube.test/watch?v=first"}]}
            if target == "https://youtube.test/watch?v=first":
                return {"title": "First result", "url": "https://stream.test/first"}
            return None

        adapter._extract = extract

        track = await adapter.resolve(TrackRequest("song name"))

        self.assertEqual("First result", track.title)
        self.assertEqual("https://stream.test/first", track.stream_url)
        self.assertEqual(
            ["ytsearch1:song name", "https://youtube.test/watch?v=first"], calls
        )

    async def test_resolves_a_url_without_treating_it_as_a_search_query(self):
        adapter = YtdlpMediaAdapter(logger=None)
        calls = []

        async def extract(target, **kwargs):
            calls.append(target)
            return {"title": "Direct result", "url": "https://stream.test/direct"}

        adapter._extract = extract

        track = await adapter.resolve(TrackRequest("HTTPS://youtube.test/watch?v=direct"))

        self.assertEqual("Direct result", track.title)
        self.assertEqual(["HTTPS://youtube.test/watch?v=direct"], calls)
