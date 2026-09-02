"""/connect flow: pick a service, paste a token (deleted), store it encrypted
per-user, disconnect. Uses aiogram FSM for the token step.
"""
from __future__ import annotations

import structlog
from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from src.bot.keyboards.connections import (
    create_connect_catalog_keyboard,
    create_my_connections_keyboard,
)
from src.connections import catalog
from src.connections.store import ConnectionsStore

logger = structlog.get_logger()
router = Router()


class ConnectStates(StatesGroup):
    awaiting_token = State()


def parse_connect_data(data: str) -> tuple[str, str]:
    """`connect:<action>[:<service_id>]` → (action, service_id)."""
    parts = (data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    sid = parts[2] if len(parts) > 2 else ""
    return action, sid


def service_help_text(service_id: str) -> str:
    svc = catalog.get_service(service_id)
    if svc is None:
        return ""
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(svc.how_to_steps))
    return (
        f"<b>{svc.icon} {svc.name}</b>\n{svc.description}\n\n"
        f"<b>Как получить {svc.secret_label}:</b>\n{steps}\n\n"
        f"Ссылка: {svc.how_to_url}\n\n"
        f"Пришлите {svc.secret_label} следующим сообщением — оно будет сразу удалено."
    )


def _store() -> ConnectionsStore | None:
    return getattr(router, "connections_store", None)


def _disabled_text() -> str:
    return (
        "Подключения выключены на этом сервере (не задан CONNECTIONS_SECRET_KEY). "
        "Обратитесь к администратору."
    )


async def _safe_edit(message, text, **kwargs) -> None:
    """edit_text that swallows Telegram's "message is not modified" error.

    Re-pressing a button that re-renders the identical text+markup (e.g.
    catalog→catalog or list→list) would otherwise raise TelegramBadRequest.
    """
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.message(Command("connect"))
async def cmd_connect(message: Message) -> None:
    if _store() is None:
        await message.answer(_disabled_text())
        return
    await message.answer(
        "<b>Подключение сервисов</b>\n\nВыберите сервис:",
        reply_markup=create_connect_catalog_keyboard(),
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("connect:"))
async def on_connect_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.data or not callback.message:
        return
    store = _store()
    if store is None:
        await callback.answer(_disabled_text(), show_alert=True)
        return
    action, sid = parse_connect_data(callback.data)
    user_id = callback.from_user.id if callback.from_user else 0

    if action == "catalog":
        await _safe_edit(
            callback.message,
            "<b>Подключение сервисов</b>\n\nВыберите сервис:",
            reply_markup=create_connect_catalog_keyboard(),
            parse_mode="HTML",
        )
    elif action == "list":
        ids = store.list_service_ids(user_id)
        text = "Ваши подключения:" if ids else "У вас пока нет подключений."
        await _safe_edit(
            callback.message,
            text,
            reply_markup=create_my_connections_keyboard(ids),
            parse_mode="HTML",
        )
    elif action == "pick":
        if catalog.get_service(sid) is None:
            await callback.answer("Неизвестный сервис")
            return
        await state.set_state(ConnectStates.awaiting_token)
        await state.update_data(service_id=sid)
        await _safe_edit(callback.message, service_help_text(sid), parse_mode="HTML")
    elif action == "disconnect":
        removed = store.disconnect(user_id, sid)
        svc = catalog.get_service(sid)
        name = svc.name if svc else sid
        await callback.answer(f"{name}: отключено" if removed else "Не найдено")
        ids = store.list_service_ids(user_id)
        await _safe_edit(
            callback.message,
            "Ваши подключения:" if ids else "У вас пока нет подключений.",
            reply_markup=create_my_connections_keyboard(ids),
            parse_mode="HTML",
        )
    await callback.answer()


@router.message(ConnectStates.awaiting_token)
async def on_token(message: Message, state: FSMContext) -> None:
    store = _store()
    data = await state.get_data()
    sid = data.get("service_id", "")
    await state.clear()
    token = (message.text or "").strip()
    bot: Bot | None = getattr(router, "bot", None)
    try:
        if bot is not None:
            await bot.delete_message(message.chat.id, message.message_id)
    except Exception:  # noqa: BLE001 — deletion is best-effort
        pass
    if store is None or not token or catalog.get_service(sid) is None:
        await message.answer("Не удалось подключить сервис. Попробуйте /connect ещё раз.")
        return
    user_id = message.from_user.id if message.from_user else 0
    store.connect(user_id=user_id, service_id=sid, secret=token)
    svc = catalog.get_service(sid)
    await message.answer(
        f"✅ {svc.name} подключён. Инструменты появятся в следующем сообщении Claude.",
        reply_markup=create_my_connections_keyboard(store.list_service_ids(user_id)),
    )


def setup_connect_handlers(dp_router: Router, *, connections_store, bot: Bot) -> None:
    router.connections_store = connections_store  # type: ignore
    router.bot = bot  # type: ignore
    dp_router.include_router(router)
