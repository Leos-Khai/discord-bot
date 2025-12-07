# Discord Bot User Guide

## Overview
A `discord.py` bot with music playback, per-guild notifications (YouTube + Twitch), channel-linking tools, and lightweight admin/general commands. Python 3.12, MongoDB for persistence, and optional YouTube/Twitch API keys for notifications.

## Features
- **Music**: `!join`, `!play <url|query>`, `!search <query>`, `!queue`, `!skip`, `!remove <pos>`, `!volume <0-150>`, `!pause`, `!resume`, `!stop`, `!seek`.
- **Notifications**: `!notifications channel #text`, `!notifications youtube add <channel|url|@handle> [#target]`, `!notifications twitch add <user|url> [#target]`, list/remove variants. YouTube posts new uploads; Twitch posts a “Watch Stream” link and edits to “Watch VOD” when offline.
- **Admin / Linking**: `!link_channel #text "Voice Name" @role`, `!list_links`, `!update_channel`, `!remove_channel`, `!set_message <type> <message>`.
- **General**: `!ping`, `!calculate <a> <op> <b>`.

## Setup
1. Install Python 3.12 and MongoDB (or point to an existing Mongo instance).
2. Create and activate a virtualenv, then install deps:
   ```bash
   python -m venv .venv
   .\.venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN` (required)
   - `BOT_PREFIX` (e.g., `!`)
   - `MONGODB_URI`, `MONGODB_DATABASE`
   - Optional notifications: `YOUTUBE_API_KEY`, `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`
4. Run the bot:
   ```bash
   python src/main.py
   ```

## Docker
```bash
docker build -t discord-bot .
docker run -d --name discord-bot -v %cd%\\src:/app/src discord-bot
```

## Notifications Quickstart
1) Set a default channel: `!notifications channel #alerts`  
2) Add YouTube: `!notifications youtube add https://youtube.com/@handle` (or channel ID/URL)  
3) Add Twitch: `!notifications twitch add https://twitch.tv/username`  
YouTube will only alert uploads after you add the channel. Twitch edits the live post to a VOD link when the stream ends.

## Troubleshooting
- Bot silent: check `DISCORD_TOKEN`, intents/permissions, and Mongo connection.
- Music issues: ensure `ffmpeg` is installed and on PATH.
- Notifications: confirm YouTube/Twitch API keys; see `src/discord_bot.log` for errors.

## License
MIT
