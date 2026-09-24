# Discord Bot User Guide

## Overview
A `discord.py` bot with music playback, text-to-speech, YouTube and Twitch notifications, voice-channel join/leave announcements, and a few general commands. It runs on Python 3.13, stores its settings in MongoDB, and uses optional YouTube and Twitch API keys for notifications.

## Features
Music and TTS commands are hybrid: they work with the prefix (`!play`) and as slash commands (`/play`). Slash commands sync when the bot starts. Every other command works with the prefix only.

### Music
- `!join`: join your current voice channel.
- `!play <url|playlist|query>`: play a YouTube URL, a playlist, or the top search result.
- `!search <query>`: search YouTube and pick a result.
- `!queue`, `!np`, `!source`: show the queue, the current track, or the current track's source details.
- `!skip`, `!pause`, `!resume`, `!stop`: control playback. `!stop` also disconnects the bot.
- `!seek <position>`: jump to a point (`78`, `1:30`, `1:00:00`) or shift by an amount (`+30`, `-1:00`).
- `!remove <number|clear>`: remove one track, or empty the queue.
- `!volume [0-150]`: set the server's volume, or show it when no number is given. The setting is saved.

### Text-to-speech
- `!tts <message>`: speak a message in your voice channel. It plays over any music without stopping it.
- `!listvoice [language]`: list available voices, optionally filtered by language (`en`, `en-GB`).
- `!setttsvoice <voice>`: choose your voice for this server, using an ID from `!listvoice`.

### Command channels (admin)
Music and TTS commands only work in the text channels you allow. Until you add at least one channel, both are turned off.
- `!music_channels`: list the allowed channels.
- `!music_channels add #channel [#channel ...]`
- `!music_channels remove #channel [#channel ...]`
- `!music_channels clear`

### Notifications (admin)
- `!notifications channel #text`: set the default channel for notifications.
- `!notifications youtube add <channel_id|url|@handle> [#target]`, plus `remove <channel_id>` and `list`. `yt` works as a short form of `youtube`.
- `!notifications twitch add <username|url> [#target]`, plus `remove <username>` and `list`.

YouTube posts new uploads that appear after you subscribe. Twitch posts a "Watch Stream" link when a stream goes live, then edits the same message to "Watch VOD" when it ends.

### Voice announcements (admin)
Link a voice channel to a text channel, and the bot posts there when members join, leave, or move between voice channels.
- `!link_channel #text "Voice Name" [@role]`: create a link, optionally with a role.
- `!list_links`, `!remove_channel`: list links, or pick one to remove.
- `!update_channel "Voice Name" #new-text`, `!update_role "Voice Name" @role`, `!remove_role "Voice Name"`: change a link.
- `!set_message <join|leave|move> <message>`: customize the message using tokens like `$USER`, `$MENTION`, `$CHANNEL`, `$OLD_CHANNEL`, and `$NEW_CHANNEL`. `!set_message reset <type|all>` restores the defaults.

### General
- `!ping`: show the bot's latency.
- `!calculate <a> <+|-|*|/> <b>`: basic arithmetic.
- `!help [command]`: show help for any command.

## Setup
1. Install Python 3.13, [uv](https://docs.astral.sh/uv/), and `ffmpeg` (it must be on your PATH). Have a MongoDB instance ready, local or remote.
2. Install dependencies:
   ```bash
   uv sync
   ```
3. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` (required)
   - `BOT_PREFIX` (defaults to `!`)
   - `MONGODB_URI`, `MONGODB_DATABASE` (required)
   - Optional, for notifications: `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
4. Run the bot:
   ```bash
   uv run bot
   ```
   `uv run python main.py` does the same thing.
5. In your server, allow a channel for music and TTS: `!music_channels add #music`.

The bot requests all gateway intents, so turn on the privileged intents (Server Members and Message Content) for your application in the Discord Developer Portal.

## Tests
```bash
uv run pytest
```

## Notifications Quickstart
1. Set a default channel: `!notifications channel #alerts`
2. Add YouTube: `!notifications youtube add https://youtube.com/@handle` (or a channel ID or URL)
3. Add Twitch: `!notifications twitch add https://twitch.tv/username`

## Project Layout
- `main.py`: entry point. It loads `src/main.py`.
- `src/cogs/`: command groups (`music`, `tts`, `notifications`, `admin`, `general`).
- `src/events/`: event handlers for startup, joining a server, and voice state changes.
- `src/`: shared logic for playback, audio coordination, seeking, notifications, and the database.
- `tests/`: pytest suite.
- `CONTEXT.md`, `docs/adr/`: domain glossary and architecture decisions.

## Troubleshooting
- Bot doesn't respond: check `DISCORD_TOKEN`, the privileged intents, the bot's permissions, and the MongoDB connection.
- Music or TTS commands are refused: make sure the channel is allowed with `!music_channels add`.
- No sound: make sure `ffmpeg` is installed and on your PATH.
- Notifications missing: confirm the YouTube and Twitch API keys.
- Errors are logged to `logs/discord_bot.log`, which rotates daily.

## License
MIT
