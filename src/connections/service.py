"""Factory: build the ConnectionsStore from Settings (or None if disabled)."""
from __future__ import annotations

from typing import Any

import structlog

from src.connections.crypto import SecretBox
from src.connections.store import ConnectionsStore

logger = structlog.get_logger()


def build_connections_store(settings: Any) -> ConnectionsStore | None:
    """Return a ConnectionsStore, or None if CONNECTIONS_SECRET_KEY is unset.

    The store shares the sessions DB file (its own connection; table
    ``user_connections``). None ⇒ the connections feature is disabled.
    """
    key = settings.get_connections_secret_key()
    if not key:
        logger.info("connections_disabled_no_key")
        return None
    box = SecretBox(key)
    return ConnectionsStore(settings.get_session_database_path(), box)
