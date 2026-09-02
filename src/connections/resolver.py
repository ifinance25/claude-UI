"""Assemble a user's connected services into an mcp_servers dict.

Called by bot/web handlers before dispatching to the Claude bridge. Secrets are
decrypted here and live only in the returned dict (in memory). Unknown or
undecryptable services are skipped so one bad row never breaks the chat.
"""
from __future__ import annotations

from typing import Any

import structlog

from src.connections import catalog
from src.connections.store import ConnectionsStore

logger = structlog.get_logger()


def build_mcp_servers(user_id: int, store: ConnectionsStore) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    for service_id in store.list_service_ids(user_id):
        svc = catalog.get_service(service_id)
        if svc is None:
            continue
        secret = store.get_secret(user_id, service_id)
        if not secret:
            continue
        try:
            servers[service_id] = svc.build_mcp_server(secret)
        except Exception as exc:  # noqa: BLE001 — never let one service break dispatch
            logger.warning("mcp_build_failed", user_id=user_id, service_id=service_id, error=str(exc))
    return servers
