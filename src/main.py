import discord
from discord.ext import commands
import json
import os
import asyncio
from db import initialize_database

# Load config
with open("config.json") as f:
    config = json.load(f)

# Create bot
bot = commands.Bot(command_prefix=config["prefix"], intents=discord.Intents.all())


async def load_cogs():
    """Dynamically load all cogs from the cogs folder."""
    for folder in os.listdir("./cogs"):
        if os.path.isdir(f"./cogs/{folder}") and not folder.startswith("__"):
            for file in os.listdir(f"./cogs/{folder}"):
                if file.endswith(".py") and not file.startswith("__"):
                    try:
                        await bot.load_extension(f"cogs.{folder}.{file[:-3]}")
                        print(f"Successfully loaded command module: {folder}/{file}")
                    except Exception as e:
                        print(f"Failed to load command module: {folder}/{file} - {e}")


async def load_events():
    """Dynamically load all events from the events folder."""
    for file in os.listdir("./events"):
        if file.endswith(".py") and not file.startswith("__"):
            try:
                await bot.load_extension(f"events.{file[:-3]}")
                print(f"Successfully loaded event: {file}")
            except Exception as e:
                print(f"Failed to load event: {file} - {e}")


async def main():
    initialize_database()  # Initialize the database
    async with bot:
        await load_events()
        await load_cogs()
        await bot.start(config["token"])


if __name__ == "__main__":
    asyncio.run(main())
