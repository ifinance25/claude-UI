"""Admin check for the Telegram bot — parity with the web's require_admin.

Authoritative source is ``users.is_admin`` in the DB (same row the web uses).
No row / is_admin=0 → not admin (fail-closed). Any lookup error → not admin.
"""
from __future__ import annotations

import asyncio


async def is_bot_admin(session_manager, user_id) -> bool:
    """True only if the user has a ``users`` row with ``is_admin == 1``."""
    if session_manager is None or user_id is None:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    try:
        row = await asyncio.to_thread(session_manager.get_user_by_id, uid)
    except Exception:  # noqa: BLE001 — fail-closed on any DB error
        return False
    return bool(row and row.get("is_admin"))


async def is_bot_privileged(session_manager, user_id, allowed_user_ids) -> bool:
    """Rides owner subscription: whitelist OR is_admin (parity with web).

    Unlike :func:`is_bot_admin` — which gates global writes (``/model``,
    ``/config``, ``/permissions``, ``model:set``) and must stay is_admin-only —
    this is the SP2 credential gate. A caller in ``ALLOWED_USER_IDS`` (the
    owner's whitelist) rides the owner's Anthropic subscription without needing
    a per-user key, even though Telegram login yields ``is_admin=0``. Any DB
    error still fails closed via :func:`is_bot_admin`.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid in set(allowed_user_ids or ()):
        return True
    return await is_bot_admin(session_manager, user_id)
