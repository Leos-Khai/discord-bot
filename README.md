# discord-bot

Discord bot using discord.py

## Running the Docker Container

1. Build the Docker image:

   ```bash
   docker build -t discord-bot .
   ```

2. Run the Docker container:

   ```bash
   docker run -d --name discord-bot-container discord-bot
   ```

3. Check the logs to ensure the bot is running:

   ```bash
   docker logs discord-bot-container
   ```
