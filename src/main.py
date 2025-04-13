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
    for file in os.listdir(cogs_dir):
        if file.endswith(".py") and not file.startswith("__"):
            try:
                await bot.load_extension(f"cogs.{file[:-3]}")
                logger.info(f"Successfully loaded cog: {file}")
            except Exception as e:
                logger.error(f"Failed to load cog: {file} - {e}")


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
    try:
        initialize_database()  # Initialize the database
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return

    async with bot:
        try:
            await load_events()
            logger.info("Events loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load events: {e}")

        try:
            await load_cogs()
            logger.info("Cogs loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load cogs: {e}")

        try:
            await bot.start(config["token"])
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")


if __name__ == "__main__":
    asyncio.run(main())
