import discord
from discord.ext import commands
import json
import os
import asyncio
from db import initialize_database
from logger import get_logger

# Get the directory of the current script
script_dir = os.path.dirname(os.path.abspath(__file__))

# Load config
with open(os.path.join(script_dir, "config.json")) as f:
    config = json.load(f)

# Create bot
bot = commands.Bot(command_prefix=config["prefix"], intents=discord.Intents.all())

logger = get_logger()


async def load_cogs():
    """Dynamically load all cogs from the cogs folder."""
    cogs_dir = os.path.join(script_dir, "cogs")
    for folder in os.listdir(cogs_dir):
        if os.path.isdir(os.path.join(cogs_dir, folder)) and not folder.startswith("__"):
            for file in os.listdir(os.path.join(cogs_dir, folder)):
                if file.endswith(".py") and not file.startswith("__"):
                    try:
                        await bot.load_extension(f"cogs.{folder}.{file[:-3]}")
                        logger.info(
                            f"Successfully loaded command module: {folder}/{file}"
                        )
                    except Exception as e:
                        logger.error(
                            f"Failed to load command module: {folder}/{file} - {e}"
                        )


async def load_events():
    """Dynamically load all events from the events folder."""
    events_dir = os.path.join(script_dir, "events")
    for file in os.listdir(events_dir):
        if file.endswith(".py") and not file.startswith("__"):
            try:
                await bot.load_extension(f"events.{file[:-3]}")
                logger.info(f"Successfully loaded event: {file}")
            except Exception as e:
                logger.error(f"Failed to load event: {file} - {e}")


async def main():
    initialize_database()  # Initialize the database
    async with bot:
        await load_events()
        await load_cogs()
        await bot.start(config["token"])


if __name__ == "__main__":
    asyncio.run(main())
