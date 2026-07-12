# Discord Bot

This context defines the concepts the bot uses to manage Discord guilds and their media playback.

## Language

**Guild Playback**:
The playback activity and ordered music selection associated with one Discord guild, from an accepted request until playback finishes or stops.
_Avoid_: music state, player

**Guild Audio Coordination**:
The policy that combines Guild Playback and a guild's queued spoken messages. When a spoken message is accepted, it is delivered over Guild Playback; if playback is queued but idle, its next track starts so the message is delivered over it. When Guild Playback is paused, spoken messages play without resuming it, and a current spoken message continues alone. Resuming Guild Playback starts it beneath any current spoken message. When Guild Playback begins while a spoken message is playing alone, the message continues over the newly started playback. Skipping a track does not discard the current spoken message; it continues over the next track or alone. Stopping or disconnecting guild audio ends both Guild Playback and every pending spoken message, and neither resumes after a later connection. A failed spoken message does not interrupt Guild Playback.
_Avoid_: voice client ownership, music/TTS coordination

**Playback Volume**:
The saved loudness preference applied to a guild's Guild Playback.
_Avoid_: volume file, music volume

**Twitch Notification Delivery**:
The attempt to notify one guild that a particular Twitch stream has gone live or offline. It is pending until delivered, or abandoned for that transition after its retry limit is exhausted; a later stream or transition is a new delivery.
_Avoid_: stream status, notification retry

**Guild Voice Announcement**:
A text announcement caused by a non-bot member joining, leaving, or moving between voice channels in one guild. A move produces one announcement when both voice channels map to the same text channel; otherwise, it produces a leave announcement and a join announcement.
_Avoid_: voice event, channel notification
