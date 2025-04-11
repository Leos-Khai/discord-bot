import logging
from datetime import datetime

# Set up logging configuration
logging.basicConfig(
    filename="discord_bot.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger():
    return logging.getLogger()
