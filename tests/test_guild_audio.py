import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from guild_audio import GuildAudioCoordinator, _GuildAudioMixer
from guild_playback import Track


class RecordingAudio:
    def __init__(self):
        self.events = []
        self.start_positions = []
        self.track_after = None
        self.speech_complete = None
        self.disconnected = False
        self.fail_speech = False
        self.fail_playback = False

    async def ensure_connected(self, channel):
        self.events.append(("connect", channel))

    async def start_playback(self, track, volume, after, start_at=0.0):
        if self.fail_playback:
            raise RuntimeError("voice unavailable")
        self.events.append(("playback", track.title, volume))
        self.start_positions.append(start_at)
        self.track_after = after

    async def start_speech(self, request, complete):
        if self.fail_speech:
            raise RuntimeError("speech unavailable")
        self.events.append(("speech", request.message))
        self.speech_complete = complete

    def pause_playback(self):
        self.events.append(("pause",))

    def resume_playback(self):
        self.events.append(("resume",))

    def skip_playback(self):
        self.events.append(("skip",))
        if self.track_after:
            self.track_after(None)

    def set_volume(self, value):
        self.events.append(("volume", value))

    async def stop_all(self):
        self.events.append(("stop",))

    async def disconnect(self):
        self.disconnected = True
        self.events.append(("disconnect",))


class StubSource:
    def __init__(self, frames=(), on_read=None):
        self.frames = list(frames)
        self.on_read = on_read
        self.cleaned = False

    def read(self):
        if self.on_read:
            self.on_read()
        return self.frames.pop(0) if self.frames else b""

    def cleanup(self):
        self.cleaned = True


class GuildAudioMixerTests(unittest.TestCase):
    def test_a_source_displaced_mid_read_neither_ends_nor_completes_its_replacement(self):
        mixer = _GuildAudioMixer()
        completions = []
        replacement = StubSource(frames=[b"\x00" * 3840])

        # The seek lands while the audio thread is inside the outgoing source's read.
        def seek():
            mixer.set_music(replacement, lambda error: completions.append(("new", error)))

        displaced = StubSource(on_read=seek)
        mixer.set_music(displaced, lambda error: completions.append(("old", error)))

        frame = mixer.read()

        self.assertNotEqual(b"", frame)
        self.assertEqual([], completions)

    def test_a_displaced_source_is_torn_down_without_completing(self):
        mixer = _GuildAudioMixer()
        completions = []
        displaced = StubSource(frames=[b"\x00" * 3840] * 5)
        mixer.set_music(displaced, lambda error: completions.append(("old", error)))

        mixer.set_music(StubSource(frames=[b"\x00" * 3840]), lambda error: None)
        mixer.read()

        self.assertTrue(displaced.cleaned)
        self.assertEqual([], completions)

    def test_the_current_source_running_dry_completes_it(self):
        mixer = _GuildAudioMixer()
        completions = []
        mixer.set_music(StubSource(), lambda error: completions.append(("current", error)))

        frame = mixer.read()

        self.assertEqual(b"", frame)
        self.assertEqual([("current", None)], completions)


class GuildAudioCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.audio = RecordingAudio()
        self.coordinator = GuildAudioCoordinator(self.audio)

    async def test_starts_guild_playback_under_a_spoken_message_already_in_progress(self):
        accepted = await self.coordinator.enqueue_speech(
            "user-1", "voice-1", "hello", bypass_cooldown=True
        )
        await self.coordinator.play(Track("song", "stream://song"), 1.0, lambda error: None)

        self.assertTrue(accepted.accepted)
        self.assertEqual([("speech", "hello"), ("playback", "song", 1.0)], self.audio.events)

    async def test_pauses_only_guild_playback_while_speech_can_continue(self):
        await self.coordinator.play(Track("song", "stream://song"), 1.0, lambda error: None)

        self.coordinator.pause()
        accepted = await self.coordinator.enqueue_speech(
            "user-1", "voice-1", "hello", bypass_cooldown=True
        )

        self.assertTrue(accepted.accepted)
        self.assertTrue(self.coordinator.is_paused())
        self.assertEqual([("playback", "song", 1.0), ("pause",), ("speech", "hello")], self.audio.events)

        self.coordinator.resume()

        self.assertEqual(("resume",), self.audio.events[-1])

    async def test_starts_queued_guild_playback_when_speech_is_accepted(self):
        async def start_queued():
            await self.coordinator.play(Track("queued song", "stream://queued"), 1.0, lambda error: None)

        self.coordinator.register_queued_playback_starter(start_queued)

        await self.coordinator.enqueue_speech("user-1", "voice-1", "hello", bypass_cooldown=True)

        self.assertEqual([("speech", "hello"), ("playback", "queued song", 1.0)], self.audio.events)

    async def test_skip_keeps_the_spoken_message_and_stop_discards_it(self):
        await self.coordinator.play(Track("song", "stream://song"), 1.0, lambda error: None)
        await self.coordinator.enqueue_speech("user-1", "voice-1", "hello", bypass_cooldown=True)

        speech_complete = self.audio.speech_complete
        self.coordinator.stop()
        self.assertEqual(("skip",), self.audio.events[-1])
        self.assertIs(speech_complete, self.audio.speech_complete)

        await self.coordinator.play(Track("next", "stream://next"), 1.0, lambda error: None)
        self.assertIs(speech_complete, self.audio.speech_complete)
        self.assertEqual(("playback", "next", 1.0), self.audio.events[-1])

        await self.coordinator.close()

        self.assertTrue(self.audio.disconnected)
        self.assertIn(("stop",), self.audio.events)

        rejected = await self.coordinator.enqueue_speech("user-2", "voice-2", "later", bypass_cooldown=True)
        self.assertFalse(rejected.accepted)

    async def test_speech_failure_does_not_interrupt_guild_playback(self):
        await self.coordinator.play(Track("song", "stream://song"), 1.0, lambda error: None)
        self.audio.fail_speech = True

        result = await self.coordinator.enqueue_speech("user-1", "voice-1", "hello", bypass_cooldown=True)

        self.assertFalse(result.accepted)
        self.assertTrue(self.coordinator.is_playing())
        self.assertEqual(("playback", "song", 1.0), self.audio.events[0])

    async def test_restarting_a_track_at_a_new_position_keeps_playback_paused(self):
        await self.coordinator.play(Track("song", "stream://song"), 1.0, lambda error: None)
        self.coordinator.pause()

        await self.coordinator.play(
            Track("song", "stream://song"), 1.0, lambda error: None, start_at=90.0
        )

        self.assertTrue(self.coordinator.is_paused())
        self.assertFalse(self.coordinator.is_playing())
        self.assertEqual([0.0, 90.0], self.audio.start_positions)

    async def test_playback_start_failure_leaves_the_coordinator_idle(self):
        self.audio.fail_playback = True

        with self.assertRaisesRegex(RuntimeError, "voice unavailable"):
            await self.coordinator.play(Track("song", "stream://song"), 1.0, lambda error: None)

        self.assertFalse(self.coordinator.is_playing())
