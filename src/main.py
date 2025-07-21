import discord
from discord.ext import commands
import json
import os
import asyncio
from dotenv import load_dotenv
from db import initialize_database
from logger import get_logger

# Load environment variables
load_dotenv()

# Get the directory of the current script
script_dir = os.path.dirname(os.path.abspath(__file__))

# Load config from environment variables with fallback to config.json
def load_config():
    # Try to load from environment variables first
    token = os.getenv('DISCORD_TOKEN')
    prefix = os.getenv('BOT_PREFIX', '!')
    mongodb_uri = os.getenv('MONGODB_URI')
    mongodb_database = os.getenv('MONGODB_DATABASE')
    
    if token and mongodb_uri and mongodb_database:
        return {
            "token": token,
            "prefix": prefix,
            "mongodb": {
                "uri": mongodb_uri,
                "database": mongodb_database
            }
        }
    else:
        # Fallback to config.json if environment variables are not set
        try:
            with open(os.path.join(script_dir, "config.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            raise ValueError("Neither environment variables nor config.json found. Please set up your configuration.")

config = load_config()


# Create bot and disable default command logging
class CustomBot(commands.Bot):
    async def on_command(self, ctx):
        pass  # Override to disable default command logging


bot = CustomBot(command_prefix=config["prefix"], intents=discord.Intents.all())

logger = get_logger()


async def load_cogs():
    """Dynamically load all cogs from the cogs folder."""
    cogs_dir = os.path.join(script_dir, "cogs")
    # Load cogs sequentially directly within the loop for clearer error reporting
    for file in os.listdir(cogs_dir):
        if file.endswith(".py") and not file.startswith("__"):
            cog_name = f"cogs.{file[:-3]}"  # Store cog name for logging
            try:
                await bot.load_extension(cog_name)
                logger.info(f"Successfully loaded extension: {cog_name}")
            except commands.ExtensionAlreadyLoaded:
                logger.warning(f"Extension {cog_name} already loaded.")
            except commands.ExtensionNotFound:
                logger.error(f"Extension {cog_name} not found.")
            except commands.NoEntryPointError:
                logger.error(f"Extension {cog_name} does not have a setup function.")
            except commands.ExtensionFailed as e:
                # This catches errors happening *inside* the cog's setup or __init__
                logger.error(
                    f"Extension {cog_name} failed to load: {e.original.__class__.__name__} - {e.original}"
                )
            except Exception as e:
                # Catch any other unexpected errors during loading attempt
                logger.error(
                    f"Failed to load extension {cog_name}: {e.__class__.__name__} - {e}"
                )


async def load_events():
    """Dynamically load all events from the events folder."""
    events_dir = os.path.join(script_dir, "events")
    load_tasks = []
    for file in os.listdir(events_dir):
        if file.endswith(".py") and not file.startswith("__"):
            try:
                load_tasks.append(bot.load_extension(f"events.{file[:-3]}"))
                logger.info(f"Queued event for loading: {file}")
            except Exception as e:
                logger.error(f"Failed to queue event: {file} - {e}")
    if load_tasks:
        await asyncio.gather(*load_tasks)


async def main():
    try:
        await initialize_database()  # Initialize the database
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        return

    async with bot:
        try:
            # Load events and cogs sequentially to handle dependencies
            await load_events()
            await load_cogs()
            logger.info("All extensions loaded successfully.")
        except Exception as e:
            # This might catch errors from the sequential loading loop above
            # Or errors during event loading
            logger.error(f"Failed during extension loading process: {e}")

        try:
            await bot.start(config["token"])
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")


if __name__ == "__main__":
    asyncio.run(main())
