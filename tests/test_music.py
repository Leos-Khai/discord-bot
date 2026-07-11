import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import discord

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from cogs.music import DiscordVoiceAdapter, YtdlpMediaAdapter
from guild_playback import Track, TrackRequest


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


class FakeVoiceClient:
    def __init__(self):
        self.source = None

    def is_connected(self):
        return True

    def play(self, source, after):
        self.source = source


class FakeGuild:
    def __init__(self):
        self.voice_client = FakeVoiceClient()


class FakeFFmpegSource(discord.AudioSource):
    def __init__(self, *args, **kwargs):
        self._current_error = RuntimeError("stream unavailable")

    def cleanup(self):
        pass

    def read(self):
        return b""


class DiscordVoiceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_exposes_an_ffmpeg_error_through_the_volume_wrapped_source(self):
        guild = FakeGuild()
        adapter = DiscordVoiceAdapter(guild)
        track = Track(title="Test track", stream_url="https://stream.test/failing")

        with patch("cogs.music.discord.FFmpegPCMAudio", FakeFFmpegSource):
            await adapter.play(track, 1.0, lambda error: None)

        self.assertEqual("stream unavailable", str(guild.voice_client.source._current_error))
