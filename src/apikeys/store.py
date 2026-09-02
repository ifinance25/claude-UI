"""SQLite store for per-user Anthropic API keys (encrypted at rest).

Follows the raw-sqlite3 + lock + WAL pattern used by ``SessionManager`` and
``ConnectionsStore``: a single long-lived connection with WAL journaling so it
can coexist with the sessions/connections DBs. Keys are encrypted via
:class:`ApiKeyCrypto` before they ever touch disk; only a ``last4`` fragment is
stored in the clear so the UI can show which key is configured without
decryption.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from src.apikeys.crypto import ApiKeyCrypto


class ApiKeyStore:
    def __init__(self, db_path: str | Path, crypto: ApiKeyCrypto):
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._crypto = crypto
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._initialize_db()

    def _connection(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(self._path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        self._conn = conn
        return conn

    def _initialize_db(self) -> None:
        with self._lock:
            self._connection().executescript(
                """
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    user_id INTEGER PRIMARY KEY,
                    key_encrypted TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    last4 TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._connection().commit()

    def set_key(self, user_id: int, key: str, *, status: str = "active") -> None:
        """Encrypt and persist ``key`` for ``user_id`` (insert or update)."""
        now = datetime.now().isoformat()
        enc = self._crypto.encrypt(key)
        last4 = key[-4:]
        with self._lock:
            self._connection().execute(
                """
                INSERT INTO user_api_keys
                    (user_id, key_encrypted, status, last4, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    key_encrypted = excluded.key_encrypted,
                    status = excluded.status,
                    last4 = excluded.last4,
                    updated_at = excluded.updated_at
                """,
                (user_id, enc, status, last4, now, now),
            )
            self._connection().commit()

    def get_key(self, user_id: int) -> str | None:
        """Return the decrypted key for ``user_id``, or ``None`` if absent/undecryptable."""
        with self._lock:
            row = self._connection().execute(
                "SELECT key_encrypted FROM user_api_keys WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return self._crypto.decrypt(row["key_encrypted"])

    def has_key(self, user_id: int) -> bool:
        """True if a key row exists for ``user_id`` (no decryption performed)."""
        with self._lock:
            row = self._connection().execute(
                "SELECT 1 FROM user_api_keys WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return row is not None

    def get_meta(self, user_id: int) -> dict | None:
        """Return non-secret metadata for ``user_id`` (never the key itself).

        A trial decrypt detects a stored-but-undecryptable key (e.g. after a
        ``CONNECTIONS_SECRET_KEY`` rotation): in that case the reported status is
        overridden to ``needs_reentry`` so the UI can prompt for a fresh key. The
        decrypted value is only tested for ``None`` — it is never returned nor
        logged. ``last4`` and timestamps are preserved regardless.
        """
        with self._lock:
            row = self._connection().execute(
                "SELECT key_encrypted, status, last4, created_at, updated_at "
                "FROM user_api_keys WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        status = row["status"]
        if self._crypto.decrypt(row["key_encrypted"]) is None:
            status = "needs_reentry"
        return {
            "status": status,
            "last4": row["last4"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def delete_key(self, user_id: int) -> bool:
        """Delete the key for ``user_id``; return True if a row was removed."""
        with self._lock:
            cur = self._connection().execute(
                "DELETE FROM user_api_keys WHERE user_id = ?",
                (user_id,),
            )
            self._connection().commit()
        return (cur.rowcount or 0) > 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
