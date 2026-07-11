# Discord Bot

This context defines the concepts the bot uses to manage Discord guilds and their media playback.

## Language

**Guild Playback**:
The playback activity and ordered music selection associated with one Discord guild, from an accepted request until playback finishes or stops.
_Avoid_: music state, player

**Playback Volume**:
The saved loudness preference applied to a guild's Guild Playback.
_Avoid_: volume file, music volume

**Twitch Notification Delivery**:
The attempt to notify one guild that a particular Twitch stream has gone live or offline. It is pending until delivered, or abandoned for that transition after its retry limit is exhausted; a later stream or transition is a new delivery.
_Avoid_: stream status, notification retry
