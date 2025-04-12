import logging
import os
from datetime import datetime

# Set up logging configuration
script_dir = os.path.dirname(os.path.abspath(__file__))
log_file = os.path.join(script_dir, "discord_bot.log")
logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def get_logger():
    return logging.getLogger()
