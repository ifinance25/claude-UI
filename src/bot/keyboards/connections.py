"""Inline keyboards for the /connect flow, driven by the connections catalog."""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.connections import catalog


def create_connect_catalog_keyboard() -> InlineKeyboardMarkup:
    """One button per catalog service (callback_data connect:pick:<id>)."""
    rows = [
        [InlineKeyboardButton(text=f"{s.icon} {s.name}", callback_data=f"connect:pick:{s.id}")]
        for s in catalog.CATALOG
    ]
    rows.append(
        [InlineKeyboardButton(text="Мои подключения", callback_data="connect:list")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_my_connections_keyboard(connected_ids: list[str]) -> InlineKeyboardMarkup:
    """List connected services with a disconnect button each, plus a catalog link."""
    rows: list[list[InlineKeyboardButton]] = []
    for sid in connected_ids:
        svc = catalog.get_service(sid)
        label = svc.name if svc else sid
        rows.append(
            [InlineKeyboardButton(text=f"Отключить {label}", callback_data=f"connect:disconnect:{sid}")]
        )
    rows.append(
        [InlineKeyboardButton(text="＋ Подключить сервис", callback_data="connect:catalog")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
