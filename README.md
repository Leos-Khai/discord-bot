# Discord Bot User Guide

## Overview
This bot is built using `discord.py` and provides various functionalities, including music playback, administrative tools, and general commands. It is designed to enhance your Discord server experience.

---

## Features

### 1. Music Commands
- **Join Voice Channel**: `!join`
- **Play Music**: `!play <URL>`
- **Search and Play**: `!search <query>`
- **Queue Management**: `!queue`, `!skip`, `!remove <position>`
- **Volume Control**: `!volume <0-150>`
- **Pause/Resume/Stop**: `!pause`, `!resume`, `!stop`

### 2. Administrative Commands
- **Link Channels**: `!link_channel #text-channel "Voice Channel Name" @role`
- **List Links**: `!list_links`
- **Update Links**: `!update_channel "Voice Channel Name" #new-text-channel`
- **Remove Links**: `!remove_channel`
- **Set Custom Messages**: `!set_message <type> <message>`

### 3. General Commands
- **Ping**: `!ping`
- **Calculator**: `!calculate <num1> <operator> <num2>`

---

## Setup Instructions

### Prerequisites
- Python 3.12
- `discord.py` library
- Other dependencies listed in `requirements.txt`

### Running Locally
1. Clone the repository:
   ```bash
   git clone <repository-url>
   ```
2. Navigate to the project directory:
   ```bash
   cd discord-bot
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Add your bot token to `src/config.json`.
5. Run the bot:
   ```bash
   python src/main.py
   ```

### Running with Docker
1. Build the Docker image:
   ```bash
   docker build -t discord-bot .
   ```
2. Run the Docker container:
   ```bash
   docker run -d --name discord-bot-container discord-bot
   ```
3. Check logs:
   ```bash
   docker logs discord-bot-container
   ```

---

## Customization

### Setting Custom Messages
Use the `!set_message` command to customize join, leave, or move messages. Example:
```bash
!set_message join "$USER joined $CHANNEL"
```

### Linking Channels
Link text and voice channels with optional roles using `!link_channel`. Example:
```bash
!link_channel #general "Gaming Voice" @Gamers
```

---

## Troubleshooting

### Common Issues
- **Bot not responding**: Ensure the bot token is correct and the bot has necessary permissions.
- **Music playback issues**: Check if `ffmpeg` is installed and accessible.

---

## Contributing
Feel free to contribute by submitting issues or pull requests.

---

## License
This project is licensed under the MIT License.
