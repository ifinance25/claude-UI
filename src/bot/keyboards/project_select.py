"""Keyboard builders for project selection and actions."""
from __future__ import annotations

from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Max projects per page (Telegram limits inline keyboard to ~100 buttons,
# but UX is better with fewer)
PROJECTS_PER_PAGE = 8


def create_project_keyboard(
    projects: list[Path],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Create keyboard with project selection buttons and pagination.

    Args:
        projects: Full list of project paths.
        page: Current page number (0-indexed).
    """
    total = len(projects)
    total_pages = max(1, (total + PROJECTS_PER_PAGE - 1) // PROJECTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * PROJECTS_PER_PAGE
    end = min(start + PROJECTS_PER_PAGE, total)
    page_projects = projects[start:end]

    builder = InlineKeyboardBuilder()

    for project in page_projects:
        builder.button(
            text=project.name,
            # Telegram callback_data limit is 64 bytes.
            callback_data=f"project:{project.name[:50]}",
        )

    # Arrange project buttons in 2 columns
    builder.adjust(2)

    # Add pagination row if needed
    if total_pages > 1:
        nav_buttons: list[InlineKeyboardButton] = []

        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"page:{page - 1}",
                )
            )

        nav_buttons.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data="page:noop",
            )
        )

        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="Вперёд ▶️",
                    callback_data=f"page:{page + 1}",
                )
            )

        # Add nav row to the keyboard
        markup = builder.as_markup()
        markup.inline_keyboard.append(nav_buttons)
        return markup

    return builder.as_markup()


def create_cancel_keyboard() -> InlineKeyboardMarkup:
    """Create keyboard with cancel button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отмена", callback_data="cancel")]
        ]
    )


def create_close_confirm_keyboard() -> InlineKeyboardMarkup:
    """Create keyboard for close confirmation."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, закрыть", callback_data="close:confirm"),
                InlineKeyboardButton(text="Нет, оставить", callback_data="close:cancel"),
            ]
        ]
    )


def create_mcp_catalog_keyboard(
    installed_names: set[str] | None = None,
) -> InlineKeyboardMarkup:
    """Create inline keyboard for the MCP catalog."""
    installed = installed_names or set()
    rows = [
        [InlineKeyboardButton(text="Install Browser", callback_data="mcp:install:browser")],
        [InlineKeyboardButton(text="Install PostgreSQL", callback_data="mcp:install:postgres")],
        [InlineKeyboardButton(text="Install GitHub", callback_data="mcp:install:github")],
    ]

    for name in sorted(installed):
        label = {
            "github": "GitHub",
            "postgres": "PostgreSQL",
        }.get(name, name.title())
        rows.append(
            [InlineKeyboardButton(text=f"Remove {label}", callback_data=f"mcp:remove:{name}")]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_mcp_candidate_keyboard(token: str) -> InlineKeyboardMarkup:
    """Create confirmation keyboard for an auto-detected MCP link."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Добавить", callback_data=f"mcp:pending:{token}"),
                InlineKeyboardButton(text="Скрыть", callback_data=f"mcp:dismiss:{token}"),
            ]
        ]
    )


def create_settings_keyboard(
    show_token_usage: bool,
    enable_subagent_tracking: bool,
    show_context_usage: bool = False,
    keep_log_after_response: bool = False,
) -> InlineKeyboardMarkup:
    """Create keyboard for settings."""
    token_text = "Токены: ВКЛ" if show_token_usage else "Токены: ВЫКЛ"
    token_callback = "settings:tokens:off" if show_token_usage else "settings:tokens:on"

    context_text = "Контекст: ВКЛ" if show_context_usage else "Контекст: ВЫКЛ"
    context_callback = "settings:context:off" if show_context_usage else "settings:context:on"

    subagent_text = (
        "Subagent logs: ON" if enable_subagent_tracking else "Subagent logs: OFF"
    )
    subagent_callback = (
        "settings:subagent_tracking:off"
        if enable_subagent_tracking
        else "settings:subagent_tracking:on"
    )

    keep_log_text = (
        "Лог после ответа: Показать" if keep_log_after_response else "Лог после ответа: Скрыть"
    )
    keep_log_callback = (
        "settings:keep_log:off" if keep_log_after_response else "settings:keep_log:on"
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=token_text, callback_data=token_callback)],
            [InlineKeyboardButton(text=context_text, callback_data=context_callback)],
            [InlineKeyboardButton(text=subagent_text, callback_data=subagent_callback)],
            [InlineKeyboardButton(text=keep_log_text, callback_data=keep_log_callback)],
            [InlineKeyboardButton(text="Закрыть", callback_data="settings:close")],
        ]
    )
