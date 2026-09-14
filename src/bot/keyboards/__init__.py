"""Inline keyboards."""

from src.bot.keyboards.models import create_model_keyboard
from src.bot.keyboards.project_select import (
    create_cancel_keyboard,
    create_close_confirm_keyboard,
    create_mcp_candidate_keyboard,
    create_mcp_catalog_keyboard,
    create_project_keyboard,
    create_settings_keyboard,
)

__all__ = [
    "create_project_keyboard",
    "create_cancel_keyboard",
    "create_close_confirm_keyboard",
    "create_mcp_catalog_keyboard",
    "create_mcp_candidate_keyboard",
    "create_settings_keyboard",
    "create_model_keyboard",
]
