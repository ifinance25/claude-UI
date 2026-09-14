"""Callback query handlers for inline buttons."""
from __future__ import annotations

import asyncio

from aiogram import Bot, Router
from aiogram.types import CallbackQuery

import structlog

from src.bot import onboarding
from src.bot.keyboards import create_mcp_catalog_keyboard
from src.bot.keyboards.models import create_model_keyboard
from src.bot.permissions import is_bot_admin
from src.claude import SessionManager
from src.claude.claude_settings import (
    read_claude_settings,
    write_claude_settings,
)
from src.claude.mcp import McpManager, get_mcp_manager
from src.claude.models import (
    DEFAULT_MODEL,
    model_label,
    normalize_model_id,
    valid_model_ids,
)
from src.config import Settings

logger = structlog.get_logger()

router = Router(name="callbacks")


def setup_callback_handlers(
    dp_router: Router,
    settings: Settings,
    session_manager: SessionManager,
    claude_bridge: "ClaudeBridge",
    bot: Bot,
) -> None:
    """Register callback handlers with the router."""
    router.settings = settings  # type: ignore
    router.session_manager = session_manager  # type: ignore
    router.claude_bridge = claude_bridge  # type: ignore
    router.bot = bot  # type: ignore
    router.mcp_manager = get_mcp_manager()  # type: ignore

    dp_router.include_router(router)


@router.callback_query(lambda c: c.data and c.data.startswith("project:"))
async def on_project_select(callback: CallbackQuery) -> None:
    """Handle project selection."""
    if not callback.data or not callback.message:
        return

    session_manager: SessionManager = router.session_manager  # type: ignore
    settings: Settings = router.settings  # type: ignore

    # Parse callback data: project:name
    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        await callback.answer("Неверные данные проекта")
        return

    project_name_short = parts[1]
    
    # Resolve full project path
    projects = settings.get_project_paths()
    project_path = None
    project_name = project_name_short
    
    for p in projects:
        if p.name[:50] == project_name_short:
            project_path = str(p)
            project_name = p.name  # Use full name
            break
            
    if not project_path:
        await callback.answer("Проект больше не найден. Обновите список через /projects")
        return

    topic_id = callback.message.message_thread_id

    if not topic_id:
        await callback.answer("Проект можно выбрать только внутри Топика")
        return

    # Create or update session
    await session_manager.async_create_session(
        topic_id=topic_id,
        project_path=project_path,
        project_name=project_name,
        chat_id=callback.message.chat.id,
    )

    logger.info(
        "📂 project_selected",
        topic_id=topic_id,
        project=project_name,
        path=project_path,
        user=callback.from_user.username if callback.from_user else "unknown",
    )

    await callback.answer(f"Проект: {project_name}")

    # Update message
    await callback.message.edit_text(
        onboarding.project_ready_message(project_name, project_path),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "close:confirm")
async def on_close_confirm(callback: CallbackQuery) -> None:
    """Handle session close confirmation."""
    if not callback.message:
        return

    session_manager: SessionManager = router.session_manager  # type: ignore
    claude_bridge = getattr(router, "claude_bridge", None)
    bot: Bot = router.bot  # type: ignore

    topic_id = callback.message.message_thread_id

    if not topic_id:
        await callback.answer("Ошибка: не внутри Топика")
        return

    # Close session
    if claude_bridge is not None:
        await claude_bridge.close_session(topic_id)
    await session_manager.async_close_session(topic_id)
    logger.info("🔒 session_closed_by_user", topic_id=topic_id)

    await callback.answer("Сессия закрыта")

    # Update message
    await callback.message.edit_text(
        "Сессия закрыта.\n\n"
        "Топик будет закрыт. Вы можете создать новый Топик для новой сессии.",
        parse_mode="HTML",
    )

    # Close the topic
    try:
        await bot.close_forum_topic(
            chat_id=callback.message.chat.id,
            message_thread_id=topic_id,
        )
    except Exception as e:
        logger.warning("failed_to_close_topic", error=str(e))


@router.callback_query(lambda c: c.data == "close:cancel")
async def on_close_cancel(callback: CallbackQuery) -> None:
    """Handle session close cancellation."""
    if not callback.message:
        return

    await callback.answer("Отменено")
    await callback.message.edit_text("Закрытие сессии отменено. Продолжаем работу!")


@router.callback_query(lambda c: c.data == "cancel")
async def on_cancel(callback: CallbackQuery) -> None:
    """Handle operation cancellation."""
    if not callback.message:
        return

    # Check if we have bridge access
    if not hasattr(router, "claude_bridge"):
        await callback.answer("Ошибка: мост не инициализирован")
        return

    from src.claude.bridge import ClaudeBridge
    claude_bridge: ClaudeBridge = router.claude_bridge  # type: ignore

    topic_id = callback.message.message_thread_id

    active_pids = claude_bridge.get_active_topic_pids()
    logger.info(
        "cancel_requested",
        topic_id=topic_id,
        chat_id=callback.message.chat.id,
        active_processes=active_pids,
    )

    if not topic_id:
        # Fallback: if only one active process running, cancel it
        if len(active_pids) == 1:
            topic_id = next(iter(active_pids))
            logger.info("cancel_fallback_topic", topic_id=topic_id)
        else:
            await callback.answer("Нет активной операции")
            return

    cancelled = await claude_bridge.cancel_message(topic_id)

    if cancelled:
        await callback.answer("Отменено!")
        try:
            await callback.message.edit_text("🛑 <b>Операция отменена пользователем.</b>", parse_mode="HTML")
        except Exception:
            pass
    else:
        await callback.answer("Нет активной операции для отмены")

@router.callback_query(lambda c: c.data and c.data.startswith("settings:"))
async def on_settings(callback: CallbackQuery) -> None:
    """Handle settings callbacks."""
    if not callback.data or not callback.message:
        return

    settings: Settings = router.settings  # type: ignore
    session_manager: SessionManager = router.session_manager  # type: ignore
    action = callback.data.split(":", 1)[1] if ":" in callback.data else ""
    topic_id = callback.message.message_thread_id

    # Handle toggle actions
    if action == "tokens:on":
        settings.display.show_token_usage = True
        logger.info("setting_changed", setting="show_token_usage", value=True)
        await callback.answer("Отображение токенов включено")
    elif action == "tokens:off":
        settings.display.show_token_usage = False
        logger.info("setting_changed", setting="show_token_usage", value=False)
        await callback.answer("Отображение токенов выключено")
    elif action == "context:on":
        settings.display.show_context_usage = True
        logger.info("setting_changed", setting="show_context_usage", value=True)
        await callback.answer("Отображение контекста включено")
    elif action == "context:off":
        settings.display.show_context_usage = False
        logger.info("setting_changed", setting="show_context_usage", value=False)
        await callback.answer("Отображение контекста выключено")
    elif action == "subagent_tracking:on":
        if topic_id:
            await session_manager.async_set_subagent_tracking(topic_id, True)
        logger.info("setting_changed", setting="enable_subagent_tracking", value=True)
        await callback.answer("Подробные логи сабагентов включены")
    elif action == "subagent_tracking:off":
        if topic_id:
            await session_manager.async_set_subagent_tracking(topic_id, False)
        logger.info("setting_changed", setting="enable_subagent_tracking", value=False)
        await callback.answer("Подробные логи сабагентов выключены")
    elif action == "keep_log:on":
        settings.display.keep_log_after_response = True
        logger.info("setting_changed", setting="keep_log_after_response", value=True)
        await callback.answer("Лог после ответа показывается")
    elif action == "keep_log:off":
        settings.display.keep_log_after_response = False
        logger.info("setting_changed", setting="keep_log_after_response", value=False)
        await callback.answer("Лог после ответа скрывается")
    elif action == "close":
        await callback.answer("Настройки закрыты")
        await callback.message.delete()
        return

    # Update keyboard after any toggle
    from src.bot.keyboards import create_settings_keyboard

    session = await session_manager.async_get_session(topic_id) if topic_id else None
    try:
        await callback.message.edit_reply_markup(
            reply_markup=create_settings_keyboard(
                settings.display.show_token_usage,
                session.enable_subagent_tracking if session else False,
                settings.display.show_context_usage,
                settings.display.keep_log_after_response,
            )
        )
    except Exception as e:
        if "message is not modified" not in str(e):
            logger.debug("settings_keyboard_update_failed", error=str(e))


@router.callback_query(lambda c: c.data and c.data.startswith("page:"))
async def on_page_navigate(callback: CallbackQuery) -> None:
    """Handle project list pagination."""
    if not callback.data or not callback.message:
        return

    # page:noop = the page counter button, just ignore
    page_str = callback.data.split(":", 1)[1]
    if page_str == "noop":
        await callback.answer()
        return

    try:
        page = int(page_str)
    except ValueError:
        await callback.answer("Ошибка навигации")
        return

    settings: Settings = router.settings  # type: ignore
    projects = settings.get_project_paths()

    from src.bot.keyboards import create_project_keyboard

    keyboard = create_project_keyboard(projects, page=page)

    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=keyboard)


@router.callback_query(lambda c: c.data and c.data.startswith("mcp:"))
async def on_mcp_action(callback: CallbackQuery) -> None:
    """Handle MCP catalog and detected-link callbacks."""
    if not callback.data or not callback.message:
        return

    manager: McpManager = router.mcp_manager  # type: ignore
    parts = callback.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    payload = parts[2] if len(parts) > 2 else ""

    if action == "install":
        try:
            result = manager.install_catalog_tool(payload)
        except ValueError:
            await callback.answer("Неизвестный MCP инструмент")
            return
        await callback.answer(result.message)
        await callback.message.edit_text(
            f"{result.message}\n\n{manager.describe_catalog()}",
            reply_markup=create_mcp_catalog_keyboard(manager.installed_server_names()),
            parse_mode="HTML",
        )
        return

    if action == "remove":
        result = manager.remove(payload)
        await callback.answer(result.message)
        await callback.message.edit_text(
            f"{result.message}\n\n{manager.describe_catalog()}",
            reply_markup=create_mcp_catalog_keyboard(manager.installed_server_names()),
            parse_mode="HTML",
        )
        return

    if action == "pending":
        result = manager.install_pending(payload)
        await callback.answer(result.message)
        await callback.message.edit_text(result.message, parse_mode="HTML")
        return

    if action == "dismiss":
        await callback.answer("Скрыто")
        await callback.message.edit_text("Установка MCP отменена.")
        return

    await callback.answer("Неизвестное действие")


@router.callback_query(lambda c: c.data and c.data.startswith("model:"))
async def on_model_action(callback: CallbackQuery) -> None:
    """Handle model picker taps: model:set:<pinned_id>. Admin-only write."""
    if not callback.data or not callback.message:
        return

    parts = callback.data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    model_id = parts[2] if len(parts) > 2 else ""

    if action != "set":
        await callback.answer()
        return

    session_manager = router.session_manager  # type: ignore
    user_id = callback.from_user.id if callback.from_user else None

    if not await is_bot_admin(session_manager, user_id):
        await callback.answer(
            "Сменить модель может только администратор", show_alert=True
        )
        return

    if model_id not in valid_model_ids():
        await callback.answer("Неизвестная модель")
        return

    # Read-modify-write is intentionally unlocked (last-writer-wins); the file
    # write itself is atomic (tempfile+os.replace). Matches web patch_model.
    data = await asyncio.to_thread(read_claude_settings)
    current = normalize_model_id(str(data.get("model") or DEFAULT_MODEL))
    if model_id == current:
        await callback.answer("Уже выбрана")
        return

    data["model"] = model_id
    await asyncio.to_thread(write_claude_settings, data)
    logger.info("bot_model_changed", model=model_id, user_id=user_id)

    # Clear the current topic's session so the next message starts fresh with
    # the new model (mirrors the text /model handler).
    topic_id = callback.message.message_thread_id
    if topic_id:
        claude_bridge = getattr(router, "claude_bridge", None)
        await session_manager.async_clear_session_id(topic_id)
        if claude_bridge is not None:
            claude_bridge.clear_session_cache(topic_id)

    await callback.answer(f"Модель изменена: {model_label(model_id)}")
    try:
        await callback.message.edit_text(
            f"🤖 <b>Модель Claude</b>\n\n"
            f"Текущая: <b>{model_label(model_id)}</b>\n\n"
            f"Выберите модель кнопкой ниже:",
            reply_markup=create_model_keyboard(model_id),
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001 — swallow "message is not modified"
        if "message is not modified" not in str(e):
            logger.debug("model_keyboard_update_failed", error=str(e))
