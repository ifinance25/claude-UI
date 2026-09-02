"""REST API for per-user service connections (catalog + connect/disconnect)."""
from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.connections import catalog
from src.web.dependencies import get_current_user_factory

logger = structlog.get_logger()


class ConnectIn(BaseModel):
    service_id: str
    secret: str


def _service_public(svc, connected: bool) -> dict:
    return {
        "id": svc.id, "name": svc.name, "icon": svc.icon,
        "description": svc.description, "how_to_url": svc.how_to_url,
        "how_to_steps": list(svc.how_to_steps), "secret_label": svc.secret_label,
        "connected": connected,
    }


def make_connections_router(
    *, jwt_secret: str, session_manager, connections_store,
    allowed_user_ids: list[int] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/connections", tags=["connections"])
    get_current_user = get_current_user_factory(
        jwt_secret, session_manager, set(allowed_user_ids or [])
    )

    @router.get("")
    async def list_connections(user: dict = Depends(get_current_user)) -> dict:
        if connections_store is None:
            return {"enabled": False, "services": [_service_public(s, False) for s in catalog.CATALOG]}
        uid = int(user["user_id"])
        connected = set(await asyncio.to_thread(connections_store.list_service_ids, uid))
        return {"enabled": True, "services": [_service_public(s, s.id in connected) for s in catalog.CATALOG]}

    @router.post("")
    async def connect(payload: ConnectIn, user: dict = Depends(get_current_user)) -> dict:
        if connections_store is None:
            raise HTTPException(status_code=503, detail="connections disabled")
        if catalog.get_service(payload.service_id) is None:
            raise HTTPException(status_code=422, detail="unknown service")
        secret = payload.secret.strip()
        if not secret:
            raise HTTPException(status_code=422, detail="empty secret")
        await asyncio.to_thread(
            connections_store.connect, user_id=int(user["user_id"]),
            service_id=payload.service_id, secret=secret,
        )
        logger.info("web_connection_saved", user_id=user["user_id"], service_id=payload.service_id)
        return {"ok": True}

    @router.delete("/{service_id}")
    async def disconnect(service_id: str, user: dict = Depends(get_current_user)) -> dict:
        if connections_store is None:
            raise HTTPException(status_code=503, detail="connections disabled")
        ok = await asyncio.to_thread(connections_store.disconnect, int(user["user_id"]), service_id)
        if not ok:
            raise HTTPException(status_code=404, detail="not connected")
        return {"ok": True}

    return router
