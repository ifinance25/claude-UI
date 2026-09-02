"""Message and command handlers."""

from src.bot.handlers.apikey import setup_apikey_handlers
from src.bot.handlers.callbacks import setup_callback_handlers
from src.bot.handlers.commands import setup_command_handlers
from src.bot.handlers.connect import setup_connect_handlers
from src.bot.handlers.files import setup_file_handlers
from src.bot.handlers.messages import setup_message_handlers

__all__ = [
    "setup_apikey_handlers",
    "setup_callback_handlers",
    "setup_command_handlers",
    "setup_connect_handlers",
    "setup_file_handlers",
    "setup_message_handlers",
]
