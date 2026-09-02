"""Inline keyboard for the /model picker, driven by the model registry."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.claude.models import KNOWN_MODELS, normalize_model_id


def create_model_keyboard(current_model_id: str) -> InlineKeyboardMarkup:
    """One button per known model; ✓ prefix on the current one.

    ``current_model_id`` may be a stored alias (opus/sonnet/haiku) — it is
    normalized to a pinned id before comparison.
    """
    current = normalize_model_id(current_model_id)
    rows = []
    for m in KNOWN_MODELS:
        mark = "✓ " if m["id"] == current else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{mark}{m['label']}",
                    callback_data=f"model:set:{m['id']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)
