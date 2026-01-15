# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (requires Python 3.12)
python -m venv .venv
.\.venv\Scripts\activate  # Windows
pip install -r requirements.txt

# Run the bot
python src/main.py

```

## Required Environment Variables

Copy `.env.example` to `.env` and configure:
- `DISCORD_TOKEN` (required)
- `BOT_PREFIX` (default: `!`)
- `MONGODB_URI`, `MONGODB_DATABASE` (required)
- `YOUTUBE_API_KEY` (optional, enables YouTube notifications)
- `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (optional, enables Twitch notifications)

Music playback requires `ffmpeg` on PATH.

## Architecture

**Entrypoint**: `src/main.py` loads config (env vars with fallback to `config.json`), initializes MongoDB via `src/db.py`, sets up logging via `src/logger.py`, then dynamically loads all cogs and events.

**Cogs** (`src/cogs/`): Command modules loaded as discord.py extensions.
- `music.py` - Music playback using yt-dlp and ffmpeg. Uses hybrid commands (prefix + slash). Maintains per-guild queues, volume persistence (`volumes.json`), and channel restrictions.
- `notifications.py` - YouTube/Twitch notification system with background polling tasks (5-min intervals). Requires API keys.
- `admin.py` - Channel linking, custom messages, music channel restrictions. Exports `is_admin()` decorator used by other cogs.
- `general.py` - Basic utility commands (`ping`, `calculate`).

**Events** (`src/events/`): Event listeners loaded as extensions.
- `on_ready.py` - Bot startup, retroactive server registration, slash command sync.
- `on_voice_state_update.py` - Voice channel join/leave/move notifications to linked text channels.
- `on_guild_join.py` - Server registration on join.

**Database** (`src/db.py`): MongoDB via motor (async). `DatabaseService` class manages collections:
- `servers`, `channel_links`, `custom_messages`, `music_channel_limits`
- `notification_channels`, `youtube_subscriptions`, `notified_videos`
- `twitch_subscriptions`, `twitch_stream_status`
- Metadata caches: `youtube_channel_meta`, `twitch_user_meta`

Module-level wrapper functions maintain backward compatibility. `initialize_database()` creates indexes on startup.

**Logging**: All diagnostics go through `get_logger()` from `src/logger.py`. Logs to `src/discord_bot.log`.

## Code Patterns

- Uses `discord.py` 2.5+ with `AutoShardedBot` and hybrid commands (`@commands.hybrid_command`)
- Async/await throughout; avoid blocking calls in cogs/events
- Admin commands use `@is_admin()` decorator from `cogs/admin.py`
- Per-guild state stored in instance dicts (e.g., `self.queues`, `self.current_tracks` in music cog)
- Config priority: environment variables > `config.json`

## Testing

No automated test suite. Test changes manually by running the bot in a test guild and exercising affected commands/events. Check `src/discord_bot.log` for errors.
