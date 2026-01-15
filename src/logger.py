import logging
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# Set up logging configuration
# Logs go to project_root/logs/ with daily rotation
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
logs_dir = os.path.join(project_root, "logs")
os.makedirs(logs_dir, exist_ok=True)

log_file = os.path.join(logs_dir, "discord_bot.log")

# Configure root logger
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Daily rotating file handler (rotates at midnight)
file_handler = TimedRotatingFileHandler(
    log_file,
    when="midnight",
    interval=1,
    backupCount=0,  # Keep all logs
    encoding="utf-8",
)
file_handler.suffix = "%Y-%m-%d"
file_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logger.addHandler(file_handler)


def get_logger():
    return logging.getLogger()
