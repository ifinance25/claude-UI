"""/apikey flow: set or delete the caller's own Anthropic API key.

Part of SP2 ("everyone pays with their own key"). The user runs ``/apikey`` and
pastes an ``sk-ant-...`` key in a follow-up message. That message is **deleted
immediately** so Telegram never retains the secret in the chat history. The key
is then

1. format-checked offline (:func:`validate_key_format`), and
2. live-probed against the Anthropic API (:func:`probe_key`)

before it is stored encrypted per-user via :class:`~src.apikeys.store.ApiKeyStore`.
A key that probes ``VALID`` is stored ``active``; one that only fails to probe
(network/timeout — ``UNKNOWN``) is stored ``unverified`` with a soft warning; a
key the API actively rejects (``INVALID`` / 401) keeps the user in the FSM to
retry. ``/apikey delete`` removes the stored key; ``/cancel`` exits the flow.

The store is injected as a router attribute (``router.api_key_store``) the same
way :mod:`src.bot.handlers.messages` receives it (see task 8). When no
``CONNECTIONS_SECRET_KEY`` is configured the store is ``None`` and the command
politely reports the feature is off.
"""
from __future__ import annotations

import structlog
from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from src.apikeys.validate import ProbeResult, probe_key, validate_key_format

logger = structlog.get_logger()
router = Router()


class ApiKeyFSM(StatesGroup):
    waiting_for_key = State()


def _store():
    """Return the injected ApiKeyStore, or ``None`` when the feature is off."""
    return getattr(router, "api_key_store", None)


def _disabled_text() -> str:
    return (
        "Управление API-ключами выключено на этом сервере "
        "(не задан CONNECTIONS_SECRET_KEY). Обратитесь к администратору."
    )


def _user_id(message: Message) -> int:
    return message.from_user.id if message.from_user else 0


@router.message(Command("apikey"))
async def cmd_apikey(message: Message, state: FSMContext) -> None:
    """``/apikey`` starts the set-key FSM; ``/apikey delete`` removes the key."""
    store = _store()
    if store is None:
        await message.answer(_disabled_text())
        return

    parts = (message.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""

    if arg == "delete":
        existed = store.delete_key(_user_id(message))
        if existed:
            await message.answer("✓ API key deleted")
        else:
            await message.answer("Ключ не был задан — удалять нечего.")
        return

    await message.answer(
        "Отправьте ваш Anthropic API-ключ (sk-ant-...).\n"
        "Сообщение будет автоматически удалено.\n"
        "/cancel для отмены"
    )
    await state.set_state(ApiKeyFSM.waiting_for_key)


@router.message(ApiKeyFSM.waiting_for_key, Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """``/cancel`` exits the FSM at any point during the key flow."""
    await state.clear()
    await message.answer("Cancelled")


@router.message(ApiKeyFSM.waiting_for_key)
async def on_key(message: Message, state: FSMContext) -> None:
    """Receive the pasted key: delete it, validate, probe, store."""
    # SECURITY: delete the message that contains the secret FIRST, before any
    # validation branch can return, so the key never lingers in chat history.
    delete_failed = False
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — deletion is best-effort
        logger.warning("apikey_delete_message_failed")
        delete_failed = True

    # If Telegram refused the deletion (bot lacks "Delete messages" rights in a
    # forum group, or the message is older than 48h) the pasted secret is STILL
    # visible in the chat history. Warn the user prominently so they can remove
    # it manually and rotate the key — the key is still processed regardless.
    delete_warning = (
        "⚠️ ВНИМАНИЕ: не удалось удалить ваше сообщение с ключом — "
        "оно осталось в истории чата!\n"
        "Удалите его вручную и по возможности перевыпустите (rotate) этот ключ.\n\n"
        if delete_failed
        else ""
    )

    api_key = (message.text or "").strip()

    # 1) Offline format check — cheapest, no network round-trip.
    if not validate_key_format(api_key):
        await message.answer(
            delete_warning + "❌ Invalid format (must start with sk-ant-)"
        )
        return  # stay in ApiKeyFSM.waiting_for_key

    # 2) Live probe against the Anthropic API.
    probe = await probe_key(api_key)
    if probe is ProbeResult.INVALID:
        await message.answer(
            delete_warning + "❌ API key validation failed (401 Unauthorized)"
        )
        return  # stay in ApiKeyFSM.waiting_for_key

    # VALID -> active; UNKNOWN (network/timeout) -> unverified (soft warning).
    status = "active" if probe is ProbeResult.VALID else "unverified"

    store = _store()
    if store is None:
        # Feature was turned off mid-flow; fail gracefully.
        await message.answer(delete_warning + _disabled_text())
        await state.clear()
        return

    store.set_key(_user_id(message), api_key, status=status)

    status_msg = (
        "verified" if status == "active" else "unverified (will be checked later)"
    )
    await message.answer(delete_warning + f"✓ API key saved ({status_msg})")
    await state.clear()


def setup_apikey_handlers(dp_router: Router, *, api_key_store, bot: Bot | None = None) -> None:
    """Register the /apikey router and inject the per-user key store.

    Must be included BEFORE the catch-all message handler so the FSM
    ``waiting_for_key`` handler intercepts the pasted key instead of the key
    being forwarded to Claude.
    """
    router.api_key_store = api_key_store  # type: ignore[attr-defined]
    router.bot = bot  # type: ignore[attr-defined]
    dp_router.include_router(router)
