"""SQLite store for per-user service connections (encrypted secrets).

Mirrors the raw-sqlite3 + lock pattern used by ``SessionManager``. Uses its own
connection to the DB file; WAL mode allows this to coexist with the sessions
connection. Secrets are encrypted via ``SecretBox`` before they ever touch disk.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import structlog

from src.connections.crypto import SecretBox, SecretDecryptError

logger = structlog.get_logger()


class ConnectionsStore:
    def __init__(self, db_path: str | Path, secret_box: SecretBox):
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._box = secret_box
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_schema()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        self._conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._connection().executescript(
                """
                CREATE TABLE IF NOT EXISTS user_connections (
                    user_id INTEGER NOT NULL,
                    service_id TEXT NOT NULL,
                    secret_encrypted TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'connected',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, service_id)
                );
                CREATE INDEX IF NOT EXISTS idx_user_connections_user
                    ON user_connections(user_id);
                """
            )
            self._connection().commit()

    def connect(self, *, user_id: int, service_id: str, secret: str) -> None:
        now = datetime.now().isoformat()
        enc = self._box.encrypt(secret)
        with self._lock:
            self._connection().execute(
                """
                INSERT INTO user_connections
                    (user_id, service_id, secret_encrypted, status, created_at, updated_at)
                VALUES (?, ?, ?, 'connected', ?, ?)
                ON CONFLICT(user_id, service_id) DO UPDATE SET
                    secret_encrypted = excluded.secret_encrypted,
                    status = 'connected',
                    updated_at = excluded.updated_at
                """,
                (user_id, service_id, enc, now, now),
            )
            self._connection().commit()
        logger.info("connection_saved", user_id=user_id, service_id=service_id)

    def disconnect(self, user_id: int, service_id: str) -> bool:
        with self._lock:
            cur = self._connection().execute(
                "DELETE FROM user_connections WHERE user_id = ? AND service_id = ?",
                (user_id, service_id),
            )
            self._connection().commit()
        return (cur.rowcount or 0) > 0

    def list_service_ids(self, user_id: int) -> list[str]:
        with self._lock:
            rows = self._connection().execute(
                "SELECT service_id FROM user_connections "
                "WHERE user_id = ? AND status = 'connected' ORDER BY service_id",
                (user_id,),
            ).fetchall()
        return [r["service_id"] for r in rows]

    def get_secret(self, user_id: int, service_id: str) -> str | None:
        with self._lock:
            row = self._connection().execute(
                "SELECT secret_encrypted FROM user_connections "
                "WHERE user_id = ? AND service_id = ?",
                (user_id, service_id),
            ).fetchone()
        if row is None:
            return None
        try:
            return self._box.decrypt(row["secret_encrypted"])
        except SecretDecryptError:
            logger.warning("connection_secret_undecryptable", user_id=user_id, service_id=service_id)
            return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
