"""REST /api/apikey: per-user Anthropic API key metadata + set/delete.

SP2 "everyone pays with their own key". This router lets a signed-in web user
manage the Anthropic API key their sessions run against. Keys are encrypted at
rest by :class:`~src.apikeys.store.ApiKeyStore`; the plaintext only ever leaves
the store when a session spawns Claude (see :mod:`src.event_bus.claude`).

CRITICAL: GET returns only non-secret metadata (status / last4 / timestamps),
never the full key.

Verbs:

* ``GET /api/apikey``    — key metadata (or nulls when none set).
* ``PUT /api/apikey``    — set a key: offline format check, then a live probe
  (VALID→active, INVALID→422, UNKNOWN→saved-but-unverified).
* ``DELETE /api/apikey`` — remove the caller's key (idempotent).

When ``api_key_store`` is ``None`` (``CONNECTIONS_SECRET_KEY`` unset → feature
dormant) every verb answers ``501 Not Implemented``.
"""
from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.apikeys.validate import ProbeResult, probe_key, validate_key_format
from src.web.dependencies import get_current_user_factory

logger = structlog.get_logger()


class ApiKeyIn(BaseModel):
    api_key: str


async def _resolve_privileged(user: dict, whitelist: set[int], session_manager) -> bool:
    """True for owner/admin callers (may fall back to owner credentials).

    Mirrors the privilege check the web dispatch path uses in
    :mod:`src.web.routes_ws`: whitelist membership or an admin flag (JWT claim,
    re-confirmed against the DB when available). Purely informational here — the
    UI uses it to decide whether a key is optional for this user.
    """
    try:
        uid = int(user["user_id"])
    except (KeyError, TypeError, ValueError):
        return False
    if uid in whitelist:
        return True
    if user.get("is_admin"):
        return True
    if session_manager is not None:
        row = await asyncio.to_thread(session_manager.get_user_by_id, uid)
        if row and row.get("is_admin"):
            return True
    return False


def make_apikey_router(
    *,
    jwt_secret: str,
    session_manager,
    api_key_store,
    allowed_user_ids: list[int] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/apikey", tags=["apikey"])
    whitelist = set(allowed_user_ids or [])
    get_current_user = get_current_user_factory(jwt_secret, session_manager, whitelist)

    def _require_store() -> None:
        if api_key_store is None:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="api key store not configured",
            )

    @router.get("")
    async def get_apikey(user: dict = Depends(get_current_user)) -> dict:
        _require_store()
        uid = int(user["user_id"])
        meta = await asyncio.to_thread(api_key_store.get_meta, uid)
        privileged = await _resolve_privileged(user, whitelist, session_manager)
        if meta is None:
            return {
                "status": None,
                "last4": None,
                "created_at": None,
                "updated_at": None,
                "privileged": privileged,
            }
        # Whitelist the fields we echo — never spread `meta` verbatim, so an
        # accidental future column on the store can't leak through this API.
        return {
            "status": meta["status"],
            "last4": meta["last4"],
            "created_at": meta["created_at"],
            "updated_at": meta["updated_at"],
            "privileged": privileged,
        }

    @router.put("")
    async def put_apikey(
        payload: ApiKeyIn, user: dict = Depends(get_current_user)
    ) -> dict:
        _require_store()
        uid = int(user["user_id"])
        api_key = (payload.api_key or "").strip()

        # 1) Offline format check — cheapest, no network round-trip.
        if not validate_key_format(api_key):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid API key format (must start with sk-ant-)",
            )

        # 2) Live probe. Never raises — collapses to VALID/INVALID/UNKNOWN.
        probe = await probe_key(api_key)
        if probe is ProbeResult.INVALID:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="API key failed validation (401 Unauthorized)",
            )

        if probe is ProbeResult.VALID:
            await asyncio.to_thread(
                api_key_store.set_key, uid, api_key, status="active"
            )
            logger.info("web_apikey_saved", user_id=uid, status="active")
            return {"status": "active", "message": "API key saved"}

        # UNKNOWN: couldn't confirm (timeout / network). Save it but flag it so a
        # transient outage isn't mistaken for a bad key; the UI can nudge a retry.
        await asyncio.to_thread(
            api_key_store.set_key, uid, api_key, status="unverified"
        )
        logger.info("web_apikey_saved", user_id=uid, status="unverified")
        return {
            "status": "unverified",
            "message": "API key saved",
            "warning": "Could not verify the key against the Anthropic API "
            "(network error). Saved as unverified.",
        }

    @router.delete("")
    async def delete_apikey(user: dict = Depends(get_current_user)) -> dict:
        _require_store()
        uid = int(user["user_id"])
        await asyncio.to_thread(api_key_store.delete_key, uid)
        logger.info("web_apikey_deleted", user_id=uid)
        return {"message": "API key deleted"}

    return router
