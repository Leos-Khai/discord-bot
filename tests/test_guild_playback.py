import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from guild_playback import GuildPlayback, Track, TrackRequest


class FakeVoice:
    def __init__(self):
        self.connected_to = None
        self.played = []
        self.start_positions = []
        self.after = None
        self.paused = False
        self.playing = False
        self.volume = 1.0
        self.disconnected = False

    async def ensure_connected(self, channel_id):
        self.connected_to = channel_id

    async def play(self, track, volume, after, start_at=0.0):
        self.played.append(track)
        self.start_positions.append(start_at)
        self.volume = volume
        self.after = after
        self.playing = True

    def is_playing(self):
        return self.playing and not self.paused

    def is_paused(self):
        return self.paused

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.playing = False
        if self.after:
            self.after(None)

    def finish(self, error=None):
        self.playing = False
        if self.after:
            self.after(error)

    async def disconnect(self):
        self.disconnected = True

    def set_volume(self, value):
        self.volume = value


class FakeMedia:
    def __init__(self, failing_targets=(), duration=None):
        self.failing_targets = set(failing_targets)
        self.duration = duration

    async def resolve(self, request):
        if request.target in self.failing_targets:
            raise RuntimeError(f"cannot resolve {request.target}")
        return Track(
            title=request.target,
            stream_url=f"stream://{request.target}",
            duration=self.duration,
        )

    async def playlist_entries(self, request):
        return [TrackRequest("first"), TrackRequest("second")]


class SlowPlaylistMedia(FakeMedia):
    def __init__(self):
        super().__init__()
        self.second_ready = asyncio.Event()

    async def resolve(self, request):
        if request.target == "second":
            await self.second_ready.wait()
        return await super().resolve(request)


class MultiplePlaylistMedia(FakeMedia):
    async def playlist_entries(self, request):
        if request.target == "playlist-a":
            return [TrackRequest("a1"), TrackRequest("a2")]
        return [TrackRequest("b1"), TrackRequest("b2")]


class FailingPlaylistMedia(FakeMedia):
    async def playlist_entries(self, request):
        return [TrackRequest("first"), TrackRequest("missing"), TrackRequest("third")]


class FakeVolumes:
    def __init__(self, value=1.0, fail_save=False):
        self.value = value
        self.fail_save = fail_save

    async def load(self, guild_id):
        return self.value

    async def save(self, guild_id, value):
        if self.fail_save:
            raise RuntimeError("database unavailable")
        self.value = value


class RecordingOutcomes:
    def __init__(self):
        self.items = []

    async def publish(self, outcome):
        self.items.append(outcome)


class GuildPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = FakeVoice()
        self.outcomes = RecordingOutcomes()
        self.volumes = FakeVolumes()
        self.playback = GuildPlayback(
            guild_id="guild-1",
            voice=self.voice,
            media=FakeMedia(),
            volumes=self.volumes,
            outcomes=self.outcomes,
        )

    async def asyncTearDown(self):
        await self.playback.close(disconnect=False)

    async def _playing_track(self, duration):
        playback = GuildPlayback(
            "guild-1", self.voice, FakeMedia(duration=duration), self.volumes, self.outcomes
        )
        await playback.enqueue(TrackRequest("first"), "voice-1")
        return playback

    async def test_starts_the_first_requested_track_and_advances_the_queue(self):
        started = await self.playback.enqueue(TrackRequest("first"), "voice-1")
        queued = await self.playback.enqueue(TrackRequest("second"), "voice-1")

        self.assertEqual("started", started.kind)
        self.assertEqual("queued", queued.kind)
        self.assertEqual(["first"], [track.title for track in self.voice.played])

        self.voice.finish()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(["first", "second"], [track.title for track in self.voice.played])
        self.assertEqual("started", self.outcomes.items[-1].kind)

    async def test_publishes_a_failure_when_the_voice_player_reports_a_stream_error(self):
        await self.playback.enqueue(TrackRequest("first"), "voice-1")

        self.voice.finish(RuntimeError("stream unavailable"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertIsNone(self.playback.current_track)
        self.assertEqual("failed", self.outcomes.items[-1].kind)
        self.assertEqual("stream unavailable", self.outcomes.items[-1].detail)

    async def test_starts_a_playlist_then_loads_the_remaining_tracks(self):
        outcome = await self.playback.enqueue_playlist(TrackRequest("playlist"), "voice-1")

        self.assertEqual("started", outcome.kind)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(["first"], [track.title for track in self.voice.played])
        self.assertEqual(["second"], [track.title for track in self.playback.queued_tracks])

    async def test_changes_active_volume_only_after_it_is_persisted(self):
        outcome = await self.playback.set_volume(0.5)

        self.assertEqual("volume_changed", outcome.kind)
        self.assertEqual(0.5, self.volumes.value)
        self.assertEqual(0.5, self.voice.volume)

    async def test_keeps_the_active_volume_when_persistence_fails(self):
        self.volumes.fail_save = True

        outcome = await self.playback.set_volume(0.5)

        self.assertEqual("failed", outcome.kind)
        self.assertEqual(1.0, self.volumes.value)
        self.assertEqual(1.0, self.voice.volume)

    async def test_reports_direct_resolution_failure_without_mutating_playback(self):
        playback = GuildPlayback(
            guild_id="guild-1",
            voice=self.voice,
            media=FakeMedia(failing_targets={"missing"}),
            volumes=self.volumes,
            outcomes=self.outcomes,
        )

        outcome = await playback.enqueue(TrackRequest("missing"), "voice-1")

        self.assertEqual("failed", outcome.kind)
        self.assertEqual([], self.voice.played)
        self.assertEqual((), playback.queued_tracks)
        await playback.close(disconnect=False)

    async def test_close_discards_queue_and_disconnects(self):
        await self.playback.enqueue(TrackRequest("first"), "voice-1")
        await self.playback.enqueue(TrackRequest("second"), "voice-1")

        await self.playback.close()

        self.assertTrue(self.voice.disconnected)
        self.assertEqual((), self.playback.queued_tracks)

    async def test_starts_a_queued_track_after_external_audio_finishes(self):
        self.voice.playing = True

        queued = await self.playback.enqueue(TrackRequest("first"), "voice-1")
        self.voice.playing = False
        started = await self.playback.start_queued_if_idle()

        self.assertEqual("queued", queued.kind)
        self.assertEqual("started", started.kind)
        self.assertEqual(["first"], [track.title for track in self.voice.played])

    async def test_starts_a_late_playlist_track_after_the_current_track_finishes(self):
        media = SlowPlaylistMedia()
        playback = GuildPlayback("guild-1", self.voice, media, self.volumes, self.outcomes)
        await playback.enqueue_playlist(TrackRequest("playlist"), "voice-1")
        self.voice.finish()
        await asyncio.sleep(0)

        media.second_ready.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(["first", "second"], [track.title for track in self.voice.played])
        await playback.close(disconnect=False)

    async def test_keeps_playlist_entries_in_request_order_across_multiple_playlists(self):
        playback = GuildPlayback("guild-1", self.voice, MultiplePlaylistMedia(), self.volumes, self.outcomes)
        await playback.enqueue_playlist(TrackRequest("playlist-a"), "voice-1")
        await playback.enqueue_playlist(TrackRequest("playlist-b"), "voice-1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(["a1"], [track.title for track in self.voice.played])
        self.assertEqual(["a2", "b1", "b2"], [track.title for track in playback.queued_tracks])
        await playback.close(disconnect=False)

    async def test_seek_restarts_the_current_track_at_an_absolute_point(self):
        playback = await self._playing_track(duration=300)

        outcome = await playback.seek("1:18")

        self.assertEqual("sought", outcome.kind)
        self.assertEqual([0.0, 78.0], self.voice.start_positions)
        self.assertEqual(["first", "first"], [track.title for track in self.voice.played])
        self.assertAlmostEqual(78.0, playback.position(), delta=1.0)
        await playback.close(disconnect=False)

    async def test_seek_shifts_forward_from_the_current_position(self):
        playback = await self._playing_track(duration=300)
        await playback.seek("2:00")

        outcome = await playback.seek("+30")

        self.assertEqual("sought", outcome.kind)
        self.assertAlmostEqual(150.0, self.voice.start_positions[-1], delta=1.0)
        await playback.close(disconnect=False)

    async def test_seek_shifts_backward_from_the_current_position(self):
        playback = await self._playing_track(duration=300)
        await playback.seek("2:00")

        outcome = await playback.seek("-1:00")

        self.assertEqual("sought", outcome.kind)
        self.assertAlmostEqual(60.0, self.voice.start_positions[-1], delta=1.0)
        await playback.close(disconnect=False)

    async def test_seek_backward_past_the_start_restarts_the_track(self):
        playback = await self._playing_track(duration=300)
        await playback.seek("0:10")

        outcome = await playback.seek("-1:00")

        self.assertEqual("sought", outcome.kind)
        self.assertEqual(0.0, self.voice.start_positions[-1])
        self.assertAlmostEqual(0.0, playback.position(), delta=1.0)
        await playback.close(disconnect=False)

    async def test_seek_past_the_end_is_refused_and_leaves_playback_untouched(self):
        playback = await self._playing_track(duration=225)

        outcome = await playback.seek("5:00")

        self.assertEqual("failed", outcome.kind)
        self.assertIn("03:45", outcome.detail)
        self.assertEqual([0.0], self.voice.start_positions)
        self.assertAlmostEqual(0.0, playback.position(), delta=1.0)
        await playback.close(disconnect=False)

    async def test_seek_to_exactly_the_track_length_is_refused(self):
        playback = await self._playing_track(duration=225)

        outcome = await playback.seek("3:45")

        self.assertEqual("failed", outcome.kind)
        self.assertEqual([0.0], self.voice.start_positions)
        await playback.close(disconnect=False)

    async def test_relative_seek_past_the_end_is_refused_rather_than_ending_the_track(self):
        playback = await self._playing_track(duration=225)
        await playback.seek("3:40")

        outcome = await playback.seek("+30")

        self.assertEqual("failed", outcome.kind)
        self.assertEqual([0.0, 220.0], self.voice.start_positions)
        await playback.close(disconnect=False)

    async def test_seek_is_refused_when_the_track_length_is_unknown(self):
        playback = await self._playing_track(duration=None)

        outcome = await playback.seek("1:00")

        self.assertEqual("failed", outcome.kind)
        self.assertIn("length is unknown", outcome.detail)
        self.assertEqual([0.0], self.voice.start_positions)
        await playback.close(disconnect=False)

    async def test_seek_is_refused_when_nothing_is_playing(self):
        outcome = await self.playback.seek("1:00")

        self.assertEqual("failed", outcome.kind)
        self.assertEqual([], self.voice.start_positions)

    async def test_seek_is_refused_for_an_unreadable_time_without_touching_playback(self):
        playback = await self._playing_track(duration=300)

        outcome = await playback.seek("1:70")

        self.assertEqual("failed", outcome.kind)
        self.assertIn("Invalid time", outcome.detail)
        self.assertEqual([0.0], self.voice.start_positions)
        await playback.close(disconnect=False)

    async def test_seek_while_paused_leaves_playback_paused_at_the_new_point(self):
        playback = await self._playing_track(duration=300)
        await playback.pause()

        outcome = await playback.seek("1:30")

        self.assertEqual("sought", outcome.kind)
        self.assertTrue(self.voice.is_paused())
        self.assertEqual(90.0, self.voice.start_positions[-1])
        self.assertEqual(90.0, playback.position())
        await playback.close(disconnect=False)

    async def test_a_pause_spanning_a_seek_does_not_count_as_progress(self):
        playback = await self._playing_track(duration=300)
        await playback.pause()
        playback._paused_at -= 60  # stand in for a minute already spent paused

        await playback.seek("1:30")
        await playback.resume()

        self.assertAlmostEqual(90.0, playback.position(), delta=1.0)
        await playback.close(disconnect=False)

    async def test_a_zero_length_shift_reports_the_position_without_restarting_the_track(self):
        playback = await self._playing_track(duration=300)
        await playback.seek("1:30")

        outcome = await playback.seek("+0")

        self.assertEqual("sought", outcome.kind)
        self.assertEqual("90", outcome.detail)
        self.assertEqual([0.0, 90.0], self.voice.start_positions)
        await playback.close(disconnect=False)

    async def test_skips_a_failed_playlist_entry_and_continues_loading(self):
        playback = GuildPlayback(
            "guild-1",
            self.voice,
            FailingPlaylistMedia(failing_targets={"missing"}),
            self.volumes,
            self.outcomes,
        )
        await playback.enqueue_playlist(TrackRequest("playlist"), "voice-1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(["third"], [track.title for track in playback.queued_tracks])
        self.assertIn("failed", [outcome.kind for outcome in self.outcomes.items])
        await playback.close(disconnect=False)
