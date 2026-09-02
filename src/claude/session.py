"""Session management for Claude Code using SQLAlchemy async ORM."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    event,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Session status enum
# ---------------------------------------------------------------------------


class SessionStatus(str, Enum):
    """High-level status of a Claude Code session."""

    NEW = "new"
    WORKING = "working"
    DONE = "done"
    STOPPED = "stopped"
    ERROR = "error"
    CLOSED = "closed"
    RECOVERED = "recovered"

    @property
    def emoji(self) -> str:
        """Return the display emoji for this status."""
        return _STATUS_EMOJI.get(self.value, "\u2753")


_STATUS_EMOJI: dict[str, str] = {
    SessionStatus.NEW: "\U0001f195",
    SessionStatus.WORKING: "\u2699\ufe0f",
    SessionStatus.DONE: "\u2705",
    SessionStatus.STOPPED: "\U0001f6d1",
    SessionStatus.ERROR: "\u274c",
    SessionStatus.CLOSED: "\U0001f512",
    SessionStatus.RECOVERED: "\U0001f504",
}


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

metadata = MetaData()


class Base(DeclarativeBase):
    metadata = metadata


class UserModel(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True)
    username = Column(String, nullable=True)
    is_admin = Column(Boolean, nullable=False, server_default="0")
    total_spent_usd = Column(Float, nullable=False, server_default="0")
    verbose_level = Column(Integer, nullable=False, server_default="1")

    projects = relationship("ProjectModel", back_populates="owner")


class ProjectModel(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    abspath = Column(String, nullable=False, unique=True)
    owner_user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)

    owner = relationship("UserModel", back_populates="projects")
    sessions = relationship("SessionModel", back_populates="project")


class SessionModel(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topic_id = Column(Integer, nullable=False, unique=True, index=True)
    chat_id = Column(Integer, nullable=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True)
    session_id = Column(String, nullable=True)
    session_status = Column(String, nullable=False, server_default="new")
    history_json = Column(Text, nullable=False, server_default="[]")
    created_at = Column(String, nullable=False)
    last_activity = Column(String, nullable=False)
    total_input_tokens = Column(Integer, nullable=False, server_default="0")
    total_output_tokens = Column(Integer, nullable=False, server_default="0")
    total_cache_read_tokens = Column(Integer, nullable=False, server_default="0")
    total_cache_creation_tokens = Column(Integer, nullable=False, server_default="0")
    total_cost_usd = Column(Float, nullable=False, server_default="0")
    message_count = Column(Integer, nullable=False, server_default="0")
    is_renamed = Column(Boolean, nullable=False, server_default="0")
    verbose_level = Column(Integer, nullable=False, server_default="1")
    enable_subagent_tracking = Column(Boolean, nullable=False, server_default="0")

    project = relationship("ProjectModel", back_populates="sessions")


class ActionLogModel(Base):
    __tablename__ = "action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    session_topic_id = Column(
        Integer, ForeignKey("sessions.topic_id", ondelete="CASCADE"), nullable=True, index=True
    )
    cost = Column(Float, nullable=False, server_default="0")
    timestamp = Column(String, nullable=False)
    command_type = Column(String, nullable=False)


# ---------------------------------------------------------------------------
# Dataclass kept for backward compatibility with handler code
# ---------------------------------------------------------------------------


def _generate_session_uuid() -> str:
    """Generate a 32-char hex UUID for cross-channel session identification.

    This is the *public* identifier surfaced via the web API; ``topic_id``
    remains the legacy integer key (negative for web-originated, positive
    for Telegram thread IDs).
    """
    return uuid.uuid4().hex


@dataclass
class TopicSession:
    """Represents a Claude Code session linked to a Telegram topic."""

    topic_id: int
    session_uuid: str = field(default_factory=_generate_session_uuid)
    session_id: str | None = None
    chat_id: int | None = None
    project_path: str = ""
    project_name: str = ""
    # Числовой id проекта (projects.id). None для сессий «без проекта»/scratch.
    # Нужен веб-фронту, чтобы адресовать /api/projects/{project_id}/members —
    # единственный ключ этого эндпоинта.
    project_id: int | None = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cost_usd: float = 0.0
    message_count: int = 0
    is_renamed: bool = False
    verbose_level: int = 1
    enable_subagent_tracking: bool = False
    status: SessionStatus = SessionStatus.NEW
    notes: str | None = None

    @property
    def total_tokens(self) -> int:
        return (
            self.total_input_tokens
            + self.total_output_tokens
            + self.total_cache_read_tokens
            + self.total_cache_creation_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "topic_id": self.topic_id,
            "session_uuid": self.session_uuid,
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "project_path": self.project_path,
            "project_name": self.project_name,
            "project_id": self.project_id,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_creation_tokens": self.total_cache_creation_tokens,
            "total_cost_usd": self.total_cost_usd,
            "message_count": self.message_count,
            "is_renamed": self.is_renamed,
            "verbose_level": self.verbose_level,
            "enable_subagent_tracking": self.enable_subagent_tracking,
            "status": self.status,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TopicSession":
        """Rehydrate a session from persisted state."""
        return cls(
            topic_id=data["topic_id"],
            session_uuid=data.get("session_uuid") or _generate_session_uuid(),
            session_id=data.get("session_id"),
            chat_id=data.get("chat_id"),
            project_path=data.get("project_path", ""),
            project_name=data.get("project_name", ""),
            project_id=data.get("project_id"),
            created_at=datetime.fromisoformat(data["created_at"]),
            last_activity=datetime.fromisoformat(data["last_activity"]),
            total_input_tokens=data.get("total_input_tokens", 0),
            total_output_tokens=data.get("total_output_tokens", 0),
            total_cache_read_tokens=data.get("total_cache_read_tokens", 0),
            total_cache_creation_tokens=data.get("total_cache_creation_tokens", 0),
            total_cost_usd=data.get("total_cost_usd", 0.0),
            message_count=data.get("message_count", 0),
            is_renamed=data.get("is_renamed", False),
            verbose_level=data.get("verbose_level", 1),
            enable_subagent_tracking=data.get("enable_subagent_tracking", False),
            status=SessionStatus(data.get("status", "new")),
            notes=data.get("notes"),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TopicSession":
        """Create from a SQLite query row."""
        # chat_id may not exist in older schemas — use safe access
        try:
            chat_id = row["chat_id"]
        except (IndexError, KeyError):
            chat_id = None
        try:
            session_uuid = row["session_uuid"] or _generate_session_uuid()
        except (IndexError, KeyError):
            session_uuid = _generate_session_uuid()
        try:
            notes = row["notes"]
        except (IndexError, KeyError):
            notes = None
        # project_id присутствует во всех запросах через _SESSION_SELECT_COLUMNS;
        # safe-access — на случай строк из иных SELECT (симметрично chat_id/notes).
        try:
            project_id = row["project_id"]
        except (IndexError, KeyError):
            project_id = None
        return cls(
            topic_id=row["topic_id"],
            session_uuid=session_uuid,
            session_id=row["session_id"],
            chat_id=chat_id,
            project_path=row["project_path"] or "",
            project_name=row["project_name"] or "",
            project_id=project_id,
            created_at=datetime.fromisoformat(row["created_at"]),
            last_activity=datetime.fromisoformat(row["last_activity"]),
            total_input_tokens=row["total_input_tokens"],
            total_output_tokens=row["total_output_tokens"],
            total_cache_read_tokens=row["total_cache_read_tokens"],
            total_cache_creation_tokens=row["total_cache_creation_tokens"],
            total_cost_usd=row["total_cost_usd"],
            message_count=row["message_count"],
            is_renamed=bool(row["is_renamed"]),
            verbose_level=row["verbose_level"],
            enable_subagent_tracking=bool(row["enable_subagent_tracking"]),
            status=SessionStatus(row["status"]),
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Helper: build database URLs
# ---------------------------------------------------------------------------

DEFAULT_DB_DIR = "data"
DEFAULT_DB_NAME = "sessions.db"


def _resolve_storage_path(storage_path: str | Path) -> Path:
    """Resolve a storage_path argument to an absolute .db path.

    Accepts either a full path (``data/sessions.db``) or a
    legacy ``.json`` path (automatically switches to ``.db``).
    """
    raw = Path(storage_path).expanduser()
    if raw.suffix == ".json":
        return raw.with_suffix(".db")
    return raw


def build_database_url(storage_path: str | Path | None = None) -> str:
    """Build an async-compatible SQLite URL for aiosqlite.

    *storage_path* can be the full path to the ``.db`` file (as returned by
    ``Settings.get_session_database_path()``), or ``None`` to use the default.
    Returns a URL like ``sqlite+aiosqlite:////absolute/path/to/sessions.db``.
    """
    if storage_path is None:
        db_path = Path(DEFAULT_DB_DIR).expanduser() / DEFAULT_DB_NAME
    else:
        db_path = _resolve_storage_path(storage_path)
    return f"sqlite+aiosqlite:///{db_path}"


def build_sync_database_url(storage_path: str | Path | None = None) -> str:
    """Build a synchronous SQLite URL (for Alembic migrations).

    Returns a URL like ``sqlite:////absolute/path/to/sessions.db``.
    """
    if storage_path is None:
        db_path = Path(DEFAULT_DB_DIR).expanduser() / DEFAULT_DB_NAME
    else:
        db_path = _resolve_storage_path(storage_path)
    return f"sqlite:///{db_path}"


# ---------------------------------------------------------------------------
# SessionManager (async ORM with sync compatibility layer)
# ---------------------------------------------------------------------------


def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """Enable WAL mode and foreign keys on every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class SessionManager:
    """Manages Claude Code sessions and their mapping to Telegram topics.

    All core methods are synchronous (using raw sqlite3) for maximum
    compatibility.  Async wrappers (``async_*``) delegate to
    ``asyncio.to_thread`` so handlers can call them from coroutines.

    Additionally exposes SQLAlchemy async engine and ORM metadata for callers
    that need it (Alembic, future migration to fully-async access layer).
    """

    def __init__(self, storage_path: str | Path = "data/sessions.db"):
        raw_path = Path(storage_path).expanduser()
        self.legacy_storage_path: Path | None = None

        if raw_path.suffix == ".json":
            self.legacy_storage_path = raw_path
            self.storage_path = raw_path.with_suffix(".db")
        else:
            self.storage_path = raw_path
            sibling_legacy_path = raw_path.with_name("sessions.json")
            if sibling_legacy_path.exists():
                self.legacy_storage_path = sibling_legacy_path

        self.database_url = build_database_url(self.storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

        # ------------------------------------------------------------------
        # SQLAlchemy async engine (used by initialize/close lifecycle and
        # available for future fully-async migration)
        # ------------------------------------------------------------------
        self._engine = create_async_engine(self.database_url, echo=False)
        event.listen(self._engine.sync_engine, "connect", _set_sqlite_pragmas)
        self._async_session = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

        # ------------------------------------------------------------------
        # Persistent sqlite3 connection (reused across sync operations)
        # ------------------------------------------------------------------
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.Lock()

        # ------------------------------------------------------------------
        # Synchronous bootstrap (schema + legacy migration + stale cleanup)
        # ------------------------------------------------------------------
        self._initialize_database()
        self._migrate_legacy_json_if_needed()
        self._cleanup_stale()

    # ------------------------------------------------------------------
    # Lifecycle (async) — for optional use by callers
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create tables via ORM and reload cache (optional async bootstrap).

        The constructor already creates the schema synchronously so this method
        is safe to call but not required.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("session_manager_initialized", db=self.database_url)

    async def close(self) -> None:
        """Dispose of the async engine connection pool and close the sync connection."""
        self.close_sync()
        await self._engine.dispose()

    # ------------------------------------------------------------------
    # Synchronous raw-sqlite helpers
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Return the persistent sqlite3 connection, creating it on first call.

        The connection is created with ``check_same_thread=False`` so it can be
        safely used from ``asyncio.to_thread`` worker threads.  A
        ``threading.Lock`` serialises access so only one thread mutates state at
        a time.  PRAGMAs are set once when the connection is first opened.
        """
        if self._conn is not None:
            return self._conn

        conn = sqlite3.connect(
            self.storage_path,
            timeout=30.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        self._conn = conn
        return conn

    def close_sync(self) -> None:
        """Close the persistent sqlite3 connection (idempotent)."""
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def _initialize_database(self) -> None:
        """Create the SQLite schema on first launch."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    total_spent_usd REAL NOT NULL DEFAULT 0.0,
                    verbose_level INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    abspath TEXT NOT NULL UNIQUE,
                    owner_user_id INTEGER,
                    FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_id INTEGER NOT NULL UNIQUE,
                    session_uuid TEXT,
                    chat_id INTEGER,
                    project_id INTEGER,
                    session_id TEXT,
                    session_status TEXT NOT NULL DEFAULT 'new',
                    history_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    last_activity TEXT NOT NULL,
                    total_input_tokens INTEGER NOT NULL DEFAULT 0,
                    total_output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    total_cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    total_cost_usd REAL NOT NULL DEFAULT 0.0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    is_renamed INTEGER NOT NULL DEFAULT 0,
                    verbose_level INTEGER NOT NULL DEFAULT 1,
                    enable_subagent_tracking INTEGER NOT NULL DEFAULT 0,
                    notes TEXT,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    session_topic_id INTEGER,
                    cost REAL NOT NULL DEFAULT 0.0,
                    timestamp TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id),
                    FOREIGN KEY (session_topic_id) REFERENCES sessions(topic_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS messages (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic_id INTEGER NOT NULL,
                    request_id TEXT,
                    type TEXT NOT NULL,
                    kind TEXT,
                    content TEXT,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (topic_id) REFERENCES sessions(topic_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_project_access (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    project_path TEXT NOT NULL,
                    access_level TEXT NOT NULL DEFAULT 'full',
                    UNIQUE(user_id, project_path),
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifact_dismissals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    project_path TEXT NOT NULL,
                    rel TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, project_path, rel)
                );

                -- Монотонный счётчик id локальных аккаунтов (H-8): значение
                -- только растёт, удаление юзера его не сбрасывает, поэтому
                -- освободившийся id не переиспользуется и новый юзер не
                -- наследует осиротевшие сессии.
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );

                -- SP2: per-user Anthropic API keys, encrypted at rest (Fernet).
                -- Lives in the same sessions.db; the ApiKeyStore also creates
                -- this table defensively with IF NOT EXISTS, so both entry
                -- points converge on one schema. Only last4 is stored in the
                -- clear (for the UI); the key itself is in key_encrypted.
                CREATE TABLE IF NOT EXISTS user_api_keys (
                    user_id       INTEGER PRIMARY KEY,
                    key_encrypted TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'active',
                    last4         TEXT,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_topic_id ON sessions(topic_id);
                CREATE INDEX IF NOT EXISTS idx_projects_abspath ON projects(abspath);
                CREATE INDEX IF NOT EXISTS idx_action_logs_topic_id ON action_logs(session_topic_id);
                CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic_id, event_id);
                CREATE INDEX IF NOT EXISTS idx_messages_kind ON messages(kind);
                CREATE INDEX IF NOT EXISTS idx_user_access_user ON user_project_access(user_id);
                CREATE INDEX IF NOT EXISTS idx_artifact_dismissals_user
                    ON artifact_dismissals(user_id, project_path);
                """
            )
            # Migrate: add chat_id column if missing (existing databases)
            cols = {
                row[1]
                for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "chat_id" not in cols:
                connection.execute("ALTER TABLE sessions ADD COLUMN chat_id INTEGER")
                logger.info("schema_migrated", added_column="sessions.chat_id")
            # Migrate: add session_uuid column + backfill + unique index
            if "session_uuid" not in cols:
                connection.execute("ALTER TABLE sessions ADD COLUMN session_uuid TEXT")
                logger.info("schema_migrated", added_column="sessions.session_uuid")
            if "notes" not in cols:
                connection.execute("ALTER TABLE sessions ADD COLUMN notes TEXT")
                logger.info("schema_migrated", added_column="sessions.notes")
            user_cols = {
                row[1]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "verbose_level" not in user_cols:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN verbose_level INTEGER NOT NULL DEFAULT 1"
                )
                logger.info("schema_migrated", added_column="users.verbose_level")
            if "password_hash" not in user_cols:
                connection.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
                logger.info("schema_migrated", added_column="users.password_hash")
            if "is_active" not in user_cols:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
                )
                logger.info("schema_migrated", added_column="users.is_active")
            # origin: происхождение аккаунта (H-2). Классификация Telegram vs
            # локальный больше НЕ зависит от диапазона id (современные TG-id
            # бывают > порога). Бэкфилл существующих строк: system → user_id=0,
            # local → есть password_hash, иначе telegram.
            if "origin" not in user_cols:
                connection.execute("ALTER TABLE users ADD COLUMN origin TEXT")
                connection.execute(
                    """
                    UPDATE users SET origin = CASE
                        WHEN user_id = 0 THEN 'system'
                        WHEN password_hash IS NOT NULL THEN 'local'
                        ELSE 'telegram'
                    END
                    WHERE origin IS NULL
                    """
                )
                logger.info("schema_migrated", added_column="users.origin")
            # token_version: монотонный счётчик, инкрементится при смене пароля
            # (H-3). JWT носит снимок; is_account_active отвергает токены со
            # старой версией → смена пароля немедленно отзывает старые сессии.
            if "token_version" not in user_cols:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
                )
                logger.info("schema_migrated", added_column="users.token_version")
            if "password_changed_at" not in user_cols:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN password_changed_at TEXT"
                )
                logger.info("schema_migrated", added_column="users.password_changed_at")
            # UNIQUE на username (L-14): не даём создать два локальных аккаунта
            # с одним логином (теневой аккаунт / неоднозначный resolve). Частичный
            # индекс — у Telegram-строк username = NULL (несколько NULL допустимы).
            try:
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
                    "ON users(username) WHERE username IS NOT NULL"
                )
            except sqlite3.IntegrityError:
                # Legacy-БД с уже существующими дублями логинов — не падаем на
                # старте, просто не навешиваем индекс (дубли разрулит оператор).
                logger.warning("users_username_unique_index_skipped_duplicates")
            # Migrate: add project_id to user_project_access (admin accesses CRUD)
            access_cols = {
                row[1]
                for row in connection.execute("PRAGMA table_info(user_project_access)").fetchall()
            }
            if "project_id" not in access_cols:
                connection.execute(
                    "ALTER TABLE user_project_access ADD COLUMN project_id INTEGER"
                )
                logger.info("schema_migrated", added_column="user_project_access.project_id")
                # Backfill: match project_path with projects.abspath
                connection.execute(
                    """
                    UPDATE user_project_access
                    SET project_id = (
                        SELECT id FROM projects WHERE projects.abspath = user_project_access.project_path
                    )
                    WHERE project_id IS NULL AND project_path IN (SELECT abspath FROM projects)
                    """
                )
                logger.info("project_id_backfilled")
            # Migrate: add granted_by to user_project_access (self-service sharing).
            # NULL = admin/system grant (protected from non-admin management); a user_id
            # = the user who granted it via self-service (that user can manage it).
            _cols = [
                r[1]
                for r in connection.execute("PRAGMA table_info(user_project_access)").fetchall()
            ]
            if "granted_by" not in _cols:
                connection.execute(
                    "ALTER TABLE user_project_access ADD COLUMN granted_by INTEGER"
                )
                logger.info("schema_migrated", added_column="user_project_access.granted_by")
            # Backfill any rows where session_uuid is NULL (legacy data
            # or rows inserted before this migration ran). We generate
            # one UUID per row in Python because SQLite can't.
            rows_missing = connection.execute(
                "SELECT topic_id FROM sessions WHERE session_uuid IS NULL OR session_uuid = ''"
            ).fetchall()
            for row in rows_missing:
                connection.execute(
                    "UPDATE sessions SET session_uuid = ? WHERE topic_id = ?",
                    (_generate_session_uuid(), row[0]),
                )
            if rows_missing:
                logger.info("session_uuid_backfilled", count=len(rows_missing))
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(session_uuid)"
            )
            connection.commit()

    def _migrate_legacy_json_if_needed(self) -> None:
        """Move existing JSON-backed sessions into SQLite once."""
        if not self.legacy_storage_path or not self.legacy_storage_path.exists():
            return

        with self._conn_lock:
            connection = self._get_connection()
            if connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]:
                return

        try:
            with self.legacy_storage_path.open() as file_handle:
                payload = json.load(file_handle)
        except Exception as exc:
            logger.error("legacy_sessions_load_failed", error=str(exc))
            return

        migrated = 0
        with self._conn_lock:
            connection = self._get_connection()
            for entry in payload.values():
                self._upsert_session(connection, TopicSession.from_dict(entry))
                migrated += 1
            connection.commit()

        self.legacy_storage_path.unlink(missing_ok=True)
        logger.info(
            "legacy_sessions_migrated",
            source=str(self.legacy_storage_path),
            destination=str(self.storage_path),
            migrated=migrated,
        )

    def _cleanup_stale(self) -> None:
        """Drop very old or never-used sessions on startup."""
        now = datetime.now()
        stale_topic_ids: list[int] = []

        for session in self.get_all_sessions():
            age = now - session.last_activity
            if session.message_count == 0 and age > timedelta(hours=24):
                stale_topic_ids.append(session.topic_id)
            elif age > timedelta(days=7):
                stale_topic_ids.append(session.topic_id)

        if not stale_topic_ids:
            return

        with self._conn_lock:
            connection = self._get_connection()
            for topic_id in stale_topic_ids:
                session = self._fetch_session(connection, topic_id)
                if session is None:
                    continue
                connection.execute("DELETE FROM sessions WHERE topic_id = ?", (topic_id,))
                logger.info(
                    "stale_session_removed",
                    topic_id=topic_id,
                    project=session.project_name,
                    msgs=session.message_count,
                    age_hours=round((now - session.last_activity).total_seconds() / 3600, 1),
                )
            connection.commit()

        logger.info(
            "cleanup_complete",
            removed=len(stale_topic_ids),
            remaining=len(self.get_all_sessions()),
        )

    # ------------------------------------------------------------------
    # Internal sync helpers
    # ------------------------------------------------------------------

    def _ensure_project(
        self,
        connection: sqlite3.Connection,
        project_path: str,
        project_name: str,
    ) -> int | None:
        if not project_path:
            return None

        connection.execute(
            """
            INSERT INTO projects (name, abspath)
            VALUES (?, ?)
            ON CONFLICT(abspath) DO UPDATE SET name = excluded.name
            """,
            (project_name or Path(project_path).name, project_path),
        )
        row = connection.execute(
            "SELECT id FROM projects WHERE abspath = ?",
            (project_path,),
        ).fetchone()
        return None if row is None else int(row["id"])

    def _upsert_session(self, connection: sqlite3.Connection, session: TopicSession) -> None:
        project_id = self._ensure_project(connection, session.project_path, session.project_name)
        # Отражаем вычисленный id на переданном объекте, чтобы create_session/
        # create_web_session вернули сессию с уже заполненным project_id (route
        # сразу зовёт _to_out — иначе свежесозданная сессия несла бы None).
        session.project_id = project_id
        connection.execute(
            """
            INSERT INTO sessions (
                topic_id,
                session_uuid,
                chat_id,
                project_id,
                session_id,
                session_status,
                created_at,
                last_activity,
                total_input_tokens,
                total_output_tokens,
                total_cache_read_tokens,
                total_cache_creation_tokens,
                total_cost_usd,
                message_count,
                is_renamed,
                verbose_level,
                enable_subagent_tracking,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_id) DO UPDATE SET
                session_uuid = COALESCE(sessions.session_uuid, excluded.session_uuid),
                chat_id = excluded.chat_id,
                project_id = excluded.project_id,
                session_id = excluded.session_id,
                session_status = excluded.session_status,
                last_activity = excluded.last_activity,
                total_input_tokens = excluded.total_input_tokens,
                total_output_tokens = excluded.total_output_tokens,
                total_cache_read_tokens = excluded.total_cache_read_tokens,
                total_cache_creation_tokens = excluded.total_cache_creation_tokens,
                total_cost_usd = excluded.total_cost_usd,
                message_count = excluded.message_count,
                is_renamed = excluded.is_renamed,
                verbose_level = excluded.verbose_level,
                enable_subagent_tracking = excluded.enable_subagent_tracking,
                notes = COALESCE(excluded.notes, sessions.notes)
            """,
            (
                session.topic_id,
                session.session_uuid,
                session.chat_id,
                project_id,
                session.session_id,
                session.status,
                session.created_at.isoformat(),
                session.last_activity.isoformat(),
                session.total_input_tokens,
                session.total_output_tokens,
                session.total_cache_read_tokens,
                session.total_cache_creation_tokens,
                session.total_cost_usd,
                session.message_count,
                int(session.is_renamed),
                session.verbose_level,
                int(session.enable_subagent_tracking),
                session.notes,
            ),
        )

    _SESSION_SELECT_COLUMNS = """
                sessions.topic_id,
                sessions.session_uuid,
                sessions.session_id,
                sessions.chat_id,
                sessions.project_id AS project_id,
                sessions.created_at,
                sessions.last_activity,
                sessions.total_input_tokens,
                sessions.total_output_tokens,
                sessions.total_cache_read_tokens,
                sessions.total_cache_creation_tokens,
                sessions.total_cost_usd,
                sessions.message_count,
                sessions.is_renamed,
                sessions.verbose_level,
                sessions.enable_subagent_tracking,
                sessions.session_status AS status,
                sessions.notes,
                COALESCE(projects.abspath, '') AS project_path,
                COALESCE(projects.name, '') AS project_name
    """

    def _fetch_session(
        self,
        connection: sqlite3.Connection,
        topic_id: int,
    ) -> TopicSession | None:
        row = connection.execute(
            f"""
            SELECT {self._SESSION_SELECT_COLUMNS}
            FROM sessions
            LEFT JOIN projects ON projects.id = sessions.project_id
            WHERE sessions.topic_id = ?
            """,
            (topic_id,),
        ).fetchone()
        if row is None:
            return None
        return TopicSession.from_row(row)

    def _fetch_session_by_uuid(
        self,
        connection: sqlite3.Connection,
        session_uuid: str,
    ) -> TopicSession | None:
        row = connection.execute(
            f"""
            SELECT {self._SESSION_SELECT_COLUMNS}
            FROM sessions
            LEFT JOIN projects ON projects.id = sessions.project_id
            WHERE sessions.session_uuid = ?
            """,
            (session_uuid,),
        ).fetchone()
        if row is None:
            return None
        return TopicSession.from_row(row)

    # ------------------------------------------------------------------
    # Public sync API (used by cron scheduler and other sync callers)
    # ------------------------------------------------------------------

    def get_session(self, topic_id: int) -> TopicSession | None:
        """Get session for a topic and refresh its activity timestamp."""
        now = datetime.now()
        with self._conn_lock:
            connection = self._get_connection()
            session = self._fetch_session(connection, topic_id)
            if session is None:
                return None
            connection.execute(
                "UPDATE sessions SET last_activity = ? WHERE topic_id = ?",
                (now.isoformat(), topic_id),
            )
            connection.commit()
            session.last_activity = now
            return session

    def create_session(
        self,
        topic_id: int,
        project_path: str,
        project_name: str,
        chat_id: int | None = None,
    ) -> TopicSession:
        """Create a new session for a topic."""
        session = TopicSession(
            topic_id=topic_id,
            chat_id=chat_id,
            project_path=project_path,
            project_name=project_name,
        )
        with self._conn_lock:
            connection = self._get_connection()
            self._upsert_session(connection, session)
            connection.commit()
        logger.info("session_created", topic_id=topic_id, project=project_name, chat_id=chat_id)
        return session

    def create_web_session(
        self,
        project_path: str,
        project_name: str,
        chat_id: int | None = None,
    ) -> TopicSession:
        """Atomically allocate the next negative topic_id and insert the session.

        topic_id allocation (``MIN(topic_id)-1``) and the INSERT happen
        under a single ``_conn_lock``/commit, so two concurrent web-session
        creates can't read the same ``MIN`` and collide on one topic_id
        (which previously triggered ``ON CONFLICT(topic_id) DO UPDATE`` and
        silently re-owned the first session while handing the second client
        a UUID that was never persisted).

        The new session also inherits the user's default ``verbose_level``
        from the ``users`` row (read in the same critical section), so a
        verbosity set via web Settings actually applies to new sessions.
        """
        with self._conn_lock:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT MIN(topic_id) FROM sessions WHERE topic_id < 0"
            ).fetchone()
            current_min = row[0] if row is not None else None
            topic_id = -1 if current_min is None else int(current_min) - 1

            verbose_level = 1
            if chat_id is not None:
                vrow = connection.execute(
                    "SELECT verbose_level FROM users WHERE user_id = ?",
                    (chat_id,),
                ).fetchone()
                if vrow is not None:
                    verbose_level = int(vrow["verbose_level"])

            session = TopicSession(
                topic_id=topic_id,
                chat_id=chat_id,
                project_path=project_path,
                project_name=project_name,
                verbose_level=verbose_level,
            )
            self._upsert_session(connection, session)
            connection.commit()
        logger.info(
            "web_session_created",
            topic_id=topic_id,
            project=project_name,
            chat_id=chat_id,
            verbose_level=verbose_level,
        )
        return session

    def update_session_id(self, topic_id: int, session_id: str | None) -> None:
        """Update the Claude session ID stored for a topic."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                """
                UPDATE sessions
                SET session_id = ?, last_activity = ?
                WHERE topic_id = ?
                """,
                (session_id, datetime.now().isoformat(), topic_id),
            )
            connection.commit()

    def clear_session_id(self, topic_id: int) -> None:
        """Clear the stored Claude session ID for a topic."""
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "UPDATE sessions SET session_id = NULL WHERE topic_id = ?",
                (topic_id,),
            )
            connection.commit()
            if cursor.rowcount:
                logger.info("session_id_cleared", topic_id=topic_id)

    def close_session(self, topic_id: int) -> bool:
        """Close and remove a session."""
        with self._conn_lock:
            connection = self._get_connection()
            session = self._fetch_session(connection, topic_id)
            if session is None:
                return False
            connection.execute("DELETE FROM sessions WHERE topic_id = ?", (topic_id,))
            connection.commit()
        logger.info("session_closed", topic_id=topic_id, project=session.project_name)
        return True

    def get_all_sessions(
        self, *, owner_chat_id: int | None = None
    ) -> list[TopicSession]:
        """Get all active sessions, optionally scoped to one owner.

        When ``owner_chat_id`` is provided, only sessions whose ``chat_id``
        matches are returned. The web layer always passes this so users
        cannot enumerate other users' sessions.
        """
        sql = f"""
            SELECT {self._SESSION_SELECT_COLUMNS}
            FROM sessions
            LEFT JOIN projects ON projects.id = sessions.project_id
        """
        params: tuple = ()
        if owner_chat_id is not None:
            sql += " WHERE sessions.chat_id = ?"
            params = (owner_chat_id,)
        sql += " ORDER BY sessions.topic_id"
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(sql, params).fetchall()
        return [TopicSession.from_row(row) for row in rows]

    def get_session_by_uuid(
        self,
        session_uuid: str,
        *,
        owner_chat_id: int | None = None,
        touch: bool = True,
    ) -> TopicSession | None:
        """Get a session by its public UUID.

        When ``owner_chat_id`` is provided, returns ``None`` if the session
        exists but belongs to someone else — so web routes can treat
        "not yours" the same as "not found" without leaking existence.

        ``touch`` controls whether ``last_activity`` is refreshed. Read-only
        paths (ownership checks on GET/DELETE, WS handshake) pass
        ``touch=False`` so a lookup doesn't turn into a write+commit under
        ``_conn_lock`` — activity is already bumped by ``add_usage`` on every
        real message.
        """
        if not session_uuid:
            return None
        now = datetime.now()
        with self._conn_lock:
            connection = self._get_connection()
            session = self._fetch_session_by_uuid(connection, session_uuid)
            if session is None:
                return None
            if owner_chat_id is not None and session.chat_id != owner_chat_id:
                return None
            if touch:
                connection.execute(
                    "UPDATE sessions SET last_activity = ? WHERE session_uuid = ?",
                    (now.isoformat(), session_uuid),
                )
                connection.commit()
                session.last_activity = now
            return session

    def next_negative_topic_id(self) -> int:
        """Allocate the next unused negative topic_id for web-origin sessions.

        Web sessions live in the negative range so they cannot collide
        with Telegram thread IDs (positive). Implemented as a single
        ``MIN(topic_id)-1`` SQL round-trip — independent of how many
        sessions exist.
        """
        with self._conn_lock:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT MIN(topic_id) FROM sessions WHERE topic_id < 0"
            ).fetchone()
        current_min = row[0] if row is not None else None
        if current_min is None:
            return -1
        return int(current_min) - 1

    def get_messages_for_topic(
        self,
        topic_id: int,
        *,
        since_event_id: int = 0,
        limit: int = 1000,
    ) -> list[tuple]:
        """Return persisted message rows for a topic ordered by event_id.

        Returns raw tuples ``(event_id, topic_id, request_id, type, kind,
        content, metadata_json, created_at)``. Callers can decode
        ``metadata_json`` themselves; SQLite-bound I/O stays in this
        module so the route layer doesn't touch the connection.
        """
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                """
                SELECT event_id, topic_id, request_id, type, kind,
                       content, metadata_json, created_at
                FROM messages
                WHERE topic_id = ? AND event_id > ?
                ORDER BY event_id
                LIMIT ?
                """,
                (topic_id, since_event_id, limit),
            ).fetchall()
        return [tuple(r) for r in rows]

    def set_session_notes(
        self,
        session_uuid: str,
        notes: str | None,
        *,
        owner_chat_id: int | None = None,
    ) -> bool:
        """Set notes for a session, optionally enforcing ownership.

        When ``owner_chat_id`` is provided, the UPDATE only matches rows
        where ``chat_id = owner_chat_id``. Returns True on hit, False
        otherwise (unknown UUID or wrong owner — both surface as 404 at
        the API layer).
        """
        if not session_uuid:
            return False
        sql = "UPDATE sessions SET notes = ? WHERE session_uuid = ?"
        params: tuple = (notes, session_uuid)
        if owner_chat_id is not None:
            sql += " AND chat_id = ?"
            params = (notes, session_uuid, owner_chat_id)
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(sql, params)
            connection.commit()
            return (cursor.rowcount or 0) > 0

    def search_sessions(
        self,
        query: str,
        *,
        owner_chat_id: int | None = None,
    ) -> list[TopicSession]:
        """Return sessions whose project name OR last message content matches.

        ``query`` is matched case-insensitively against:
        - ``projects.name``
        - the ``content`` of the most-recent ``messages`` row for the session
          (latest by ``event_id``)

        When ``owner_chat_id`` is set the result is restricted to that
        owner — web callers must pass the current user's id so they
        cannot enumerate other users' sessions.

        Empty/whitespace query returns all sessions (same as
        :meth:`get_all_sessions`). Result order matches ``get_all_sessions``
        (by ``topic_id`` ascending).
        """
        q = query.strip()
        if not q:
            return self.get_all_sessions(owner_chat_id=owner_chat_id)

        like = f"%{q.lower()}%"
        where_clauses = [
            "(LOWER(COALESCE(projects.name, '')) LIKE ?"
            " OR LOWER(COALESCE(last_msg.content, '')) LIKE ?)"
        ]
        params: list = [like, like]
        if owner_chat_id is not None:
            where_clauses.append("sessions.chat_id = ?")
            params.append(owner_chat_id)
        where = " AND ".join(where_clauses)
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                f"""
                SELECT {self._SESSION_SELECT_COLUMNS}
                FROM sessions
                LEFT JOIN projects ON projects.id = sessions.project_id
                LEFT JOIN messages AS last_msg ON last_msg.event_id = (
                    SELECT MAX(event_id)
                    FROM messages
                    WHERE messages.topic_id = sessions.topic_id
                )
                WHERE {where}
                ORDER BY sessions.topic_id
                """,
                tuple(params),
            ).fetchall()
        return [TopicSession.from_row(row) for row in rows]

    def has_session(self, topic_id: int) -> bool:
        """Check whether a topic already has a session."""
        with self._conn_lock:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE topic_id = ?",
                (topic_id,),
            ).fetchone()
        return row is not None

    def add_usage(
        self,
        topic_id: int,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: float = 0.0,
        command_type: str = "message",
    ) -> None:
        """Add token usage to cumulative session counters."""
        now = datetime.now()
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO users (user_id, username, is_admin, total_spent_usd, origin)
                VALUES (0, 'system', 0, 0.0, 'system')
                ON CONFLICT(user_id) DO NOTHING
                """
            )
            cursor = connection.execute(
                """
                UPDATE sessions
                SET total_input_tokens = total_input_tokens + ?,
                    total_output_tokens = total_output_tokens + ?,
                    total_cache_read_tokens = total_cache_read_tokens + ?,
                    total_cache_creation_tokens = total_cache_creation_tokens + ?,
                    total_cost_usd = total_cost_usd + ?,
                    message_count = message_count + 1,
                    last_activity = ?
                WHERE topic_id = ?
                """,
                (
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                    cost_usd,
                    now.isoformat(),
                    topic_id,
                ),
            )
            if not cursor.rowcount:
                connection.commit()
                return

            connection.execute(
                """
                INSERT INTO action_logs (user_id, session_topic_id, cost, timestamp, command_type)
                VALUES (?, ?, ?, ?, ?)
                """,
                (0, topic_id, cost_usd, now.isoformat(), command_type),
            )
            connection.execute(
                """
                UPDATE users
                SET total_spent_usd = total_spent_usd + ?
                WHERE user_id = 0
                """,
                (cost_usd,),
            )
            connection.commit()

    def mark_renamed(self, topic_id: int) -> None:
        """Mark that the topic name was already auto-renamed."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                """
                UPDATE sessions
                SET is_renamed = 1
                WHERE topic_id = ? AND is_renamed = 0
                """,
                (topic_id,),
            )
            connection.commit()

    def set_verbose_level(self, topic_id: int, level: int) -> None:
        """Set per-topic verbosity for streamed output."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                "UPDATE sessions SET verbose_level = ? WHERE topic_id = ?",
                (level, topic_id),
            )
            connection.commit()

    def get_user_default_verbose(self, user_id: int) -> int:
        """Return the user-level default verbose level (0-3).

        Stored on the ``users`` row so the value is symmetric with the
        web PATCH endpoint and independent of any particular session.
        Falls back to 1 when the user has never set it.
        """
        with self._conn_lock:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT verbose_level FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["verbose_level"]) if row is not None else 1

    def set_user_verbose(self, user_id: int, level: int) -> int:
        """Persist user-level verbose default. Returns affected row count.

        Upserts into the ``users`` row so the preference survives across
        sessions. Per-session ``sessions.verbose_level`` overrides are
        left untouched — they apply only to that specific session.
        """
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                """
                INSERT INTO users (user_id, verbose_level, origin)
                VALUES (?, ?, 'telegram')
                ON CONFLICT(user_id) DO UPDATE SET verbose_level = excluded.verbose_level
                """,
                (user_id, level),
            )
            connection.commit()
            return cursor.rowcount or 0

    def record_telegram_user(
        self, user_id: int, username: str | None = None
    ) -> None:
        """Апсерт строки ``users`` для Telegram/веб-аутентифицированного юзера.

        Чинит резолв приглашений (H-1/M-10): шеринг ищет участника по числовому
        id ИЛИ ``@username``, но строка + username раньше писались ТОЛЬКО для
        локальных (логин/пароль) аккаунтов — Telegram-login / magic-link /
        bot-middleware оставляли ``username = NULL`` (или строки не было вовсе),
        поэтому и приглашение по ``@username``, и по числовому id навсегда
        отдавали 404. Здесь строка апсертится на каждом аутентифицированном
        взаимодействии.

        - Создаёт строку с ``origin='telegram'``, если её нет (числовой id
          после первого входа резолвится).
        - Присваивает ``username`` ТОЛЬКО когда он непустой; НИКОГДА не затирает
          уже записанный username пустым/None (magic-link username не несёт).
        - Не трогает локальные аккаунты (``origin='local'``).
        - Уважает partial-unique-индекс на username: если handle уже принадлежит
          другой строке (username переиспользован в Telegram), пропускает
          присвоение, но саму строку сохраняет.
        """
        uname = (username or "").strip() or None
        with self._conn_lock:
            connection = self._get_connection()
            # 1) Гарантируем строку (идемпотентно, существующие колонки не трогаем).
            connection.execute(
                "INSERT INTO users (user_id, username, origin) "
                "VALUES (?, NULL, 'telegram') "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            # 2) Best-effort присвоение username, без затирания локального
            #    аккаунта и без нарушения unique-индекса. NOT EXISTS отсекает
            #    коллизию под _conn_lock (единственный писатель); try/except —
            #    страховка на случай гонки/legacy-строк.
            if uname is not None:
                try:
                    connection.execute(
                        "UPDATE users SET username = ? "
                        "WHERE user_id = ? "
                        "AND (origin IS NULL OR origin != 'local') "
                        "AND (username IS NULL OR username != ?) "
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM users u2 "
                        "  WHERE u2.username = ? AND u2.user_id != ?"
                        ")",
                        (uname, user_id, uname, uname, user_id),
                    )
                except sqlite3.IntegrityError:
                    pass
            connection.commit()

    # ── Локальные аккаунты (логин/пароль) и доступы к проектам ──────

    def create_local_user(
        self, *, username: str, password_hash: str, is_admin: bool
    ) -> int:
        """Создаёт локального пользователя, возвращает его user_id.

        Локальные id живут в диапазоне ≥ 1_000_000_001 и выдаются МОНОТОННО
        через счётчик ``app_meta.local_user_seq`` (H-8): удаление юзера счётчик
        не сбрасывает, поэтому освободившийся id не переиспользуется и новый
        аккаунт не наследует осиротевшие сессии. Дубликат username → ValueError
        (L-14: UNIQUE-индекс на username)."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Инициализируем счётчик (минимум 1_000_000_000) и поднимаем его
                # не ниже текущего максимума id (на случай legacy MAX+1-строк),
                # затем выдаём следующий — строго больше всех существующих и
                # всех ранее выданных.
                connection.execute(
                    "INSERT INTO app_meta(key, value) VALUES('local_user_seq', 1000000000) "
                    "ON CONFLICT(key) DO NOTHING"
                )
                connection.execute(
                    "UPDATE app_meta SET value = MAX(value, "
                    "(SELECT COALESCE(MAX(user_id), 0) FROM users)) "
                    "WHERE key = 'local_user_seq'"
                )
                connection.execute(
                    "UPDATE app_meta SET value = value + 1 WHERE key = 'local_user_seq'"
                )
                uid = int(
                    connection.execute(
                        "SELECT value FROM app_meta WHERE key = 'local_user_seq'"
                    ).fetchone()[0]
                )
                connection.execute(
                    "INSERT INTO users "
                    "(user_id, username, is_admin, is_active, password_hash, origin) "
                    "VALUES (?, ?, ?, 1, ?, 'local')",
                    (uid, username, 1 if is_admin else 0, password_hash),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValueError(f"username already exists: {username}") from exc
            connection.commit()
            return uid

    def get_user_by_username(self, username: str) -> dict | None:
        with self._conn_lock:
            connection = self._get_connection()
            r = connection.execute(
                "SELECT user_id, username, is_admin, is_active, password_hash, "
                "verbose_level, origin, token_version, password_changed_at "
                "FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if r is None:
            return None
        return {
            "user_id": r["user_id"],
            "username": r["username"],
            "is_admin": r["is_admin"],
            "is_active": r["is_active"],
            "password_hash": r["password_hash"],
            "verbose_level": r["verbose_level"],
            "origin": r["origin"],
            "token_version": r["token_version"],
            "password_changed_at": r["password_changed_at"],
        }

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._conn_lock:
            connection = self._get_connection()
            r = connection.execute(
                "SELECT user_id, username, is_admin, is_active, password_hash, "
                "verbose_level, origin, token_version, password_changed_at "
                "FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if r is None:
            return None
        return {
            "user_id": r["user_id"],
            "username": r["username"],
            "is_admin": r["is_admin"],
            "is_active": r["is_active"],
            "password_hash": r["password_hash"],
            "verbose_level": r["verbose_level"],
            "origin": r["origin"],
            "token_version": r["token_version"],
            "password_changed_at": r["password_changed_at"],
        }

    def list_users(self) -> list[dict]:
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                "SELECT user_id, username, is_admin, is_active FROM users ORDER BY user_id"
            ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "is_admin": r["is_admin"],
                "is_active": r["is_active"],
            }
            for r in rows
        ]

    def set_user_active(self, user_id: int, active: bool) -> bool:
        """Меняет is_active. Возвращает True, если пользователь существовал."""
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "UPDATE users SET is_active = ? WHERE user_id = ?",
                (1 if active else 0, user_id),
            )
            connection.commit()
        return (cursor.rowcount or 0) > 0

    def set_user_admin(self, user_id: int, is_admin: bool) -> bool:
        """Меняет роль is_admin (промоут/демоут). Возвращает True, если
        пользователь существовал. Роль перепроверяется по БД в require_admin,
        поэтому смена действует немедленно (не дожидаясь истечения JWT)."""
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "UPDATE users SET is_admin = ? WHERE user_id = ?",
                (1 if is_admin else 0, user_id),
            )
            connection.commit()
        return (cursor.rowcount or 0) > 0

    def ensure_owner_admin(self, owner_user_id: int) -> bool:
        """Bootstrap the OWNER as admin on a fresh DB (idempotent, safe).

        Telegram login yields ``is_admin=0`` and there is no automatic
        promotion, so a fresh self-host operator otherwise cannot change the
        model ("Сменить модель может только администратор") until an admin is
        set by hand via SQL. This promotes the OWNER — the first id in
        ``ALLOWED_USER_IDS`` — to ``is_admin=1`` on startup, but ONLY when the
        DB has ZERO admins. If any admin already exists (e.g. one was set
        manually on an existing deployment like prod), this is a no-op so
        later-added whitelist users (students) are never over-granted.

        Returns True if the owner was promoted, False otherwise. The
        count-then-upsert runs under a single ``BEGIN IMMEDIATE`` so it is
        atomic against concurrent writers. Telegram rows carry ``username =
        NULL`` (see idx_users_username partial index) and ``origin='telegram'``;
        an existing owner row only has its ``is_admin`` flag flipped, leaving
        username/origin intact."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                admin_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE is_admin = 1"
                ).fetchone()[0]
                if admin_count:
                    connection.commit()
                    return False
                connection.execute(
                    """
                    INSERT INTO users (user_id, username, is_admin, origin)
                    VALUES (?, NULL, 1, 'telegram')
                    ON CONFLICT(user_id) DO UPDATE SET is_admin = 1
                    """,
                    (owner_user_id,),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        logger.info("owner_bootstrapped_as_admin", user_id=owner_user_id)
        return True

    def delete_user(self, user_id: int) -> bool:
        """Удаляет пользователя. Гранты (user_project_access) уходят по FK
        ON DELETE CASCADE. Таблица artifact_dismissals не имеет FK к users,
        поэтому чистим её явно (как delete_project/rename_project) — иначе
        скрытия артефактов остаются висячими строками.

        Сессии СОХРАНЯЕМ (решение: историю не теряем), но снимаем владельца —
        ``chat_id = NULL`` (H-8). Иначе новый локальный аккаунт, получивший тот
        же id, унаследовал бы переписку удалённого. После анонимизации
        owner-проверка (``chat_id != owner``) никогда не совпадёт → сессии
        недоступны через веб, но строки целы.
        Возвращает True, если строка была."""
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "DELETE FROM users WHERE user_id = ?", (user_id,)
            )
            connection.execute(
                "DELETE FROM artifact_dismissals WHERE user_id = ?", (user_id,)
            )
            connection.execute(
                "UPDATE sessions SET chat_id = NULL WHERE chat_id = ?", (user_id,)
            )
            connection.commit()
        deleted = (cursor.rowcount or 0) > 0
        if deleted:
            logger.info("user_deleted", user_id=user_id)
        return deleted

    def set_user_password(self, user_id: int, password_hash: str) -> bool:
        """Меняет password_hash. Возвращает True, если пользователь существовал.

        Инкрементит ``token_version`` и ставит ``password_changed_at`` (H-3):
        ранее выданные JWT несут старую версию → is_account_active их отвергнет,
        то есть смена/сброс пароля немедленно отзывает все активные сессии."""
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, "
                "token_version = token_version + 1, password_changed_at = ? "
                "WHERE user_id = ?",
                (password_hash, datetime.now().isoformat(), user_id),
            )
            connection.commit()
        return (cursor.rowcount or 0) > 0

    # ── CRUD проектов (админка) ─────────────────────────────────────

    def list_all_projects(self) -> list[dict]:
        """Все проекты (для админки). ``abspath`` — путь проекта."""
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                "SELECT id, abspath FROM projects ORDER BY id"
            ).fetchall()
        return [{"id": r["id"], "abspath": r["abspath"]} for r in rows]

    def create_project(self, abspath: str) -> dict:
        """Создаёт проект по ``abspath``. ``name`` берётся из basename пути.

        Поднимает ``ValueError`` при пустом пути и ``sqlite3.IntegrityError``
        при дубликате (UNIQUE abspath) — оба ловятся на уровне роутера и
        транслируются в 400/409."""
        path = (abspath or "").strip()
        if not path:
            raise ValueError("abspath required")
        name = Path(path).name or path
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "INSERT INTO projects (name, abspath) VALUES (?, ?)",
                (name, path),
            )
            connection.commit()
            pid = int(cursor.lastrowid)
        logger.info("project_created", project_id=pid, abspath=path)
        return {"id": pid, "abspath": path}

    def _purge_artifact_dismissals(self, connection: sqlite3.Connection, abspath: str) -> None:
        """Удаляет скрытия артефактов для корня проекта (сырой и resolved пути).

        Скрытия (``artifact_dismissals``) ключуются по resolved-пути корня
        проекта (``str(access.root)``), а ``projects.abspath`` хранится «как
        ввели». При удалении/переименовании проекта чистим оба варианта, чтобы
        не копить мёртвые строки. Вызывать под ``_conn_lock`` с открытым
        ``connection``; FK к ``users`` у таблицы нет, поэтому каскад — явный.
        """
        keys = {abspath}
        try:
            keys.add(str(Path(abspath).expanduser().resolve()))
        except (OSError, ValueError):
            pass
        for key in keys:
            connection.execute(
                "DELETE FROM artifact_dismissals WHERE project_path = ?", (key,)
            )

    def rename_project(self, project_id: int, abspath: str) -> dict | None:
        """Меняет ``abspath`` существующего проекта (и производный ``name``).

        Возвращает обновлённую запись или ``None``, если проекта нет.
        Поднимает ``ValueError`` при пустом пути и ``sqlite3.IntegrityError``
        при дубликате."""
        path = (abspath or "").strip()
        if not path:
            raise ValueError("abspath required")
        name = Path(path).name or path
        with self._conn_lock:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT abspath FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return None
            old_abspath = row["abspath"]
            connection.execute(
                "UPDATE projects SET abspath = ?, name = ? WHERE id = ?",
                (path, name, project_id),
            )
            # Синхронизируем project_path грантов: доступ раздаётся/проверяется
            # по project_path (get_access_level), поэтому без этого после
            # переименования юзеры теряют доступ, а delete_project не
            # каскадит старые гранты. Обновляем и по project_id, и по старому
            # пути — чтобы покрыть и новые (с project_id), и легаси-строки.
            connection.execute(
                "UPDATE user_project_access SET project_path = ? "
                "WHERE project_id = ? OR project_path = ?",
                (path, project_id, old_abspath),
            )
            # Старые скрытия артефактов ссылались на rel под СТАРЫМ корнем —
            # после переименования они бессмысленны, вычищаем (мёртвые строки).
            self._purge_artifact_dismissals(connection, old_abspath)
            connection.commit()
        logger.info("project_renamed", project_id=project_id, abspath=path)
        return {"id": project_id, "abspath": path}

    def delete_project(self, project_id: int) -> bool:
        """Удаляет проект и каскадно его гранты в ``user_project_access``.

        FK от user_project_access ведёт на users (по user_id), а не на
        projects, поэтому каскад по abspath делаем явно. Сессии не трогаем —
        у них FK ON DELETE SET NULL на projects.id. Возвращает True, если
        строка была удалена."""
        with self._conn_lock:
            connection = self._get_connection()
            row = connection.execute(
                "SELECT abspath FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return False
            abspath = row["abspath"]
            # Каскад и по project_id, и по project_path: первое ловит гранты,
            # выданные по id, второе — легаси-строки (project_id=NULL) и любой
            # рассинхрон пути.
            connection.execute(
                "DELETE FROM user_project_access WHERE project_id = ? OR project_path = ?",
                (project_id, abspath),
            )
            # Каскадно чистим и скрытия артефактов этого проекта (FK нет).
            self._purge_artifact_dismissals(connection, abspath)
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            connection.commit()
        logger.info("project_deleted", project_id=project_id, abspath=abspath)
        return True

    def set_project_access(
        self, user_id: int, project_path: str, access_level: str,
        granted_by: int | None = None,
    ) -> None:
        with self._conn_lock:
            connection = self._get_connection()
            # Резолвим project_id по совпадению abspath, чтобы легаси-путь
            # выдачи доступа не плодил строки с project_id=NULL (иначе они
            # показываются как «—» в общем списке /accesses).
            proj = connection.execute(
                "SELECT id FROM projects WHERE abspath = ?", (project_path,)
            ).fetchone()
            project_id = proj["id"] if proj else None
            connection.execute(
                "INSERT INTO user_project_access "
                "(user_id, project_path, access_level, project_id, granted_by) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, project_path) DO UPDATE SET "
                "access_level = excluded.access_level, "
                "project_id = excluded.project_id, "
                "granted_by = excluded.granted_by",
                (user_id, project_path, access_level, project_id, granted_by),
            )
            connection.commit()

    def revoke_project_access(self, user_id: int, project_path: str) -> None:
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                "DELETE FROM user_project_access WHERE user_id = ? AND project_path = ?",
                (user_id, project_path),
            )
            connection.commit()

    def revoke_project_access_for_project(
        self, user_id: int, project_id: int, project_path: str
    ) -> None:
        """Снимает грант юзера на проект, матчась по ``project_id`` ИЛИ точному
        ``project_path`` (F3). Так revoke убирает строку, даже если она хранится
        под не-каноническим путём, но с верным project_id. Отдельный метод (не
        расширение ``revoke_project_access``), чтобы не менять контракт
        админ-роута ``revoke_access_by_path`` (тот строго по пути)."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                "DELETE FROM user_project_access "
                "WHERE user_id = ? AND (project_id = ? OR project_path = ?)",
                (user_id, project_id, project_path),
            )
            connection.commit()

    def list_project_access(self, user_id: int) -> list[dict]:
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                "SELECT project_path, access_level FROM user_project_access WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [
            {"project_path": r["project_path"], "access_level": r["access_level"]}
            for r in rows
        ]

    def get_project_access_row(
        self, user_id: int, project_id: int, project_path: str
    ) -> dict | None:
        """Строка доступа {access_level, granted_by} или None.

        F3: матч по ``project_id`` (канонический ключ) ИЛИ точному
        ``project_path`` (легаси-фолбэк для строк с project_id=NULL), чтобы грант
        под не-каноническим путём не был невидимым/недоступным для управления."""
        with self._conn_lock:
            connection = self._get_connection()
            r = connection.execute(
                "SELECT access_level, granted_by FROM user_project_access "
                "WHERE user_id = ? AND (project_id = ? OR project_path = ?)",
                (user_id, project_id, project_path),
            ).fetchone()
        if r is None:
            return None
        return {"access_level": r["access_level"], "granted_by": r["granted_by"]}

    def list_project_members(self, project_id: int, project_path: str) -> list[dict]:
        """Участники ОДНОГО проекта: [{user_id, username, access_level, granted_by}].

        F3: матч по ``project_id`` ИЛИ точному ``project_path`` — гранты под
        не-каноническим путём (легаси/рассинхрон), но с верным project_id тоже
        попадают в панель."""
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                "SELECT a.user_id, a.access_level, a.granted_by, u.username "
                "FROM user_project_access a "
                "LEFT JOIN users u ON u.user_id = a.user_id "
                "WHERE (a.project_id = ? OR a.project_path = ?) ORDER BY a.user_id",
                (project_id, project_path),
            ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "access_level": r["access_level"],
                "granted_by": r["granted_by"],
            }
            for r in rows
        ]

    def get_project_abspath(self, project_id: int) -> str | None:
        """abspath проекта по id (None если нет)."""
        with self._conn_lock:
            connection = self._get_connection()
            r = connection.execute(
                "SELECT abspath FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return r["abspath"] if r else None

    def list_tool_artifact_rows(
        self, owner_chat_id: int | None = None, limit: int = 5000
    ) -> list[dict]:
        """tool_use-сообщения с file_path → [{"tool","file_path","created_at"}].

        Источник списка артефактов (Фаза 2.3). Фильтрация «внутри какого
        проекта» выполняется в aggregate_artifacts по resolved-пути.

        ``owner_chat_id`` (M-7): когда задан — отдаём ТОЛЬКО артефакты из сессий
        этого пользователя (JOIN sessions по chat_id), чтобы в общем проекте не
        светить чужую файловую активность. ``limit`` (L-9): берём не более N
        самых свежих tool_use-строк, чтобы стоимость не росла со всей историей
        БД; затем возвращаем их в хронологическом порядке (для aggregate).
        """
        limit = max(1, int(limit))
        with self._conn_lock:
            connection = self._get_connection()
            # LIKE — лишь префильтр (сокращает скан); корректность гарантирует
            # Python-проверка ниже: json.loads в try + file_path непустая строка.
            if owner_chat_id is not None:
                rows = connection.execute(
                    "SELECT metadata_json, created_at FROM ("
                    "  SELECT m.metadata_json AS metadata_json, m.created_at AS created_at, "
                    "         m.event_id AS event_id "
                    "  FROM messages m JOIN sessions s ON m.topic_id = s.topic_id "
                    "  WHERE m.kind = 'tool_use' AND m.metadata_json LIKE '%\"file_path\"%' "
                    "        AND s.chat_id = ? "
                    "  ORDER BY m.event_id DESC LIMIT ?"
                    ") ORDER BY event_id ASC",
                    (int(owner_chat_id), limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT metadata_json, created_at FROM ("
                    "  SELECT metadata_json, created_at, event_id FROM messages "
                    "  WHERE kind = 'tool_use' AND metadata_json LIKE '%\"file_path\"%' "
                    "  ORDER BY event_id DESC LIMIT ?"
                    ") ORDER BY event_id ASC",
                    (limit,),
                ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                meta = json.loads(r["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            tool = meta.get("name") or ""
            created_at = r["created_at"] or ""
            # Bash может создать несколько файлов/папок за вызов (bash_outputs).
            # Разворачиваем их в отдельные строки артефактов; для Write/Edit —
            # одиночный file_path.
            paths: list[str] = []
            bash_outputs = meta.get("bash_outputs")
            if isinstance(bash_outputs, list):
                paths = [p for p in bash_outputs if isinstance(p, str) and p]
            if not paths:
                fp = meta.get("file_path")
                if isinstance(fp, str) and fp:
                    paths = [fp]
            for fp in paths:
                out.append(
                    {
                        "tool": tool,
                        "file_path": fp,
                        "created_at": created_at,
                    }
                )
        return out

    def add_artifact_dismissal(self, user_id: int, project_path: str, rel: str) -> None:
        """Пометить артефакт скрытым для пользователя (идемпотентно)."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                "INSERT OR IGNORE INTO artifact_dismissals (user_id, project_path, rel) "
                "VALUES (?, ?, ?)",
                (user_id, project_path, rel),
            )
            connection.commit()

    def remove_artifact_dismissal(self, user_id: int, project_path: str, rel: str) -> None:
        """Снять скрытие артефакта (вернуть в список)."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                "DELETE FROM artifact_dismissals "
                "WHERE user_id = ? AND project_path = ? AND rel = ?",
                (user_id, project_path, rel),
            )
            connection.commit()

    def list_artifact_dismissals(self, user_id: int, project_path: str) -> list[str]:
        """Список скрытых артефактов (rel) пользователя в проекте."""
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                "SELECT rel FROM artifact_dismissals "
                "WHERE user_id = ? AND project_path = ?",
                (user_id, project_path),
            ).fetchall()
        return [r["rel"] for r in rows]

    def get_access_level(self, user_id: int, project_path: str) -> str | None:
        with self._conn_lock:
            connection = self._get_connection()
            r = connection.execute(
                "SELECT access_level FROM user_project_access WHERE user_id = ? AND project_path = ?",
                (user_id, project_path),
            ).fetchone()
        return r["access_level"] if r else None

    def list_all_accesses(self) -> list[dict]:
        """List all access records with user & project info (for admin panel)."""
        with self._conn_lock:
            connection = self._get_connection()
            rows = connection.execute(
                """
                SELECT
                    a.id,
                    a.user_id,
                    u.username,
                    a.project_id,
                    p.abspath as project_path,
                    a.access_level
                FROM user_project_access a
                JOIN users u ON a.user_id = u.user_id
                LEFT JOIN projects p ON a.project_id = p.id
                ORDER BY a.id DESC
                """
            ).fetchall()
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"],
                "project_id": r["project_id"],
                "project_path": r["project_path"],
                "access_level": r["access_level"],
            }
            for r in rows
        ]

    def find_access(self, user_id: int, project_id: int) -> dict | None:
        """Найти существующий грант юзера на проект (по project_id или по
        abspath проекта). Используется для 409 на дубликат в POST /accesses.
        Возвращает None, если проекта нет или гранта нет."""
        with self._conn_lock:
            connection = self._get_connection()
            proj = connection.execute(
                "SELECT abspath FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if not proj:
                return None
            r = connection.execute(
                "SELECT id, access_level FROM user_project_access "
                "WHERE user_id = ? AND (project_id = ? OR project_path = ?)",
                (user_id, project_id, proj["abspath"]),
            ).fetchone()
            return {"id": r["id"], "access_level": r["access_level"]} if r else None

    def grant_project_access_by_id(
        self, user_id: int, project_id: int, access_level: str
    ) -> dict:
        """Grant access using project_id instead of path."""
        with self._conn_lock:
            connection = self._get_connection()
            # Get project abspath
            proj = connection.execute(
                "SELECT abspath FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if not proj:
                raise ValueError("project not found")
            project_path = proj["abspath"]
            # Insert or update
            connection.execute(
                """
                INSERT INTO user_project_access (user_id, project_id, project_path, access_level)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, project_path) DO UPDATE SET
                    access_level = excluded.access_level,
                    project_id = excluded.project_id
                """,
                (user_id, project_id, project_path, access_level),
            )
            connection.commit()
            # Return the created/updated record
            r = connection.execute(
                "SELECT id, user_id, project_id, access_level FROM user_project_access WHERE user_id = ? AND project_path = ?",
                (user_id, project_path),
            ).fetchone()
            return {
                "id": r["id"],
                "user_id": r["user_id"],
                "project_id": r["project_id"],
                "access_level": r["access_level"],
            }

    def get_access_by_id(self, access_id: int) -> dict | None:
        """Get access record by id with user & project info."""
        with self._conn_lock:
            connection = self._get_connection()
            r = connection.execute(
                """
                SELECT
                    a.id,
                    a.user_id,
                    u.username,
                    a.project_id,
                    p.abspath as project_path,
                    a.access_level
                FROM user_project_access a
                JOIN users u ON a.user_id = u.user_id
                LEFT JOIN projects p ON a.project_id = p.id
                WHERE a.id = ?
                """,
                (access_id,),
            ).fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "user_id": r["user_id"],
            "username": r["username"],
            "project_id": r["project_id"],
            "project_path": r["project_path"],
            "access_level": r["access_level"],
        }

    def update_access_level(self, access_id: int, new_level: str) -> bool:
        """Update access level for an access record."""
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "UPDATE user_project_access SET access_level = ? WHERE id = ?",
                (new_level, access_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def revoke_access_by_id(self, access_id: int) -> bool:
        """Revoke access by access record id."""
        with self._conn_lock:
            connection = self._get_connection()
            cursor = connection.execute(
                "DELETE FROM user_project_access WHERE id = ?", (access_id,)
            )
            connection.commit()
            return cursor.rowcount > 0

    def set_subagent_tracking(self, topic_id: int, enabled: bool) -> None:
        """Set whether detailed subagent logs should be streamed."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                """
                UPDATE sessions
                SET enable_subagent_tracking = ?
                WHERE topic_id = ?
                """,
                (int(enabled), topic_id),
            )
            connection.commit()

    def set_status(self, topic_id: int, status: SessionStatus | str) -> None:
        """Persist the current high-level status for a topic."""
        with self._conn_lock:
            connection = self._get_connection()
            connection.execute(
                """
                UPDATE sessions
                SET session_status = ?, last_activity = ?
                WHERE topic_id = ?
                """,
                (status, datetime.now().isoformat(), topic_id),
            )
            connection.commit()

    def get_usage(self, topic_id: int) -> dict[str, Any]:
        """Return cumulative token usage for a topic."""
        with self._conn_lock:
            connection = self._get_connection()
            row = connection.execute(
                """
                SELECT
                    total_input_tokens,
                    total_output_tokens,
                    total_cache_read_tokens,
                    total_cache_creation_tokens,
                    total_cost_usd,
                    message_count
                FROM sessions
                WHERE topic_id = ?
                """,
                (topic_id,),
            ).fetchone()

        if row is None:
            return {}

        return {
            "total_input_tokens": row["total_input_tokens"],
            "total_output_tokens": row["total_output_tokens"],
            "total_cache_read_tokens": row["total_cache_read_tokens"],
            "total_cache_creation_tokens": row["total_cache_creation_tokens"],
            "total_tokens": (
                row["total_input_tokens"]
                + row["total_output_tokens"]
                + row["total_cache_read_tokens"]
                + row["total_cache_creation_tokens"]
            ),
            "total_cost_usd": row["total_cost_usd"],
            "message_count": row["message_count"],
        }

    # ------------------------------------------------------------------
    # Async wrappers (used by bot handlers via ``await sm.async_*(...)``)
    # ------------------------------------------------------------------

    async def async_get_session(self, topic_id: int) -> TopicSession | None:
        return await asyncio.to_thread(self.get_session, topic_id)

    async def async_get_session_by_uuid(
        self,
        session_uuid: str,
        *,
        owner_chat_id: int | None = None,
        touch: bool = True,
    ) -> TopicSession | None:
        return await asyncio.to_thread(
            self.get_session_by_uuid,
            session_uuid,
            owner_chat_id=owner_chat_id,
            touch=touch,
        )

    async def async_create_session(
        self,
        topic_id: int,
        project_path: str,
        project_name: str,
        chat_id: int | None = None,
    ) -> TopicSession:
        return await asyncio.to_thread(
            self.create_session,
            topic_id,
            project_path,
            project_name,
            chat_id,
        )

    async def async_create_web_session(
        self,
        project_path: str,
        project_name: str,
        chat_id: int | None = None,
    ) -> TopicSession:
        return await asyncio.to_thread(
            self.create_web_session, project_path, project_name, chat_id
        )

    async def async_update_session_id(self, topic_id: int, session_id: str | None) -> None:
        await asyncio.to_thread(self.update_session_id, topic_id, session_id)

    async def async_clear_session_id(self, topic_id: int) -> None:
        await asyncio.to_thread(self.clear_session_id, topic_id)

    async def async_close_session(self, topic_id: int) -> bool:
        return await asyncio.to_thread(self.close_session, topic_id)

    async def async_get_all_sessions(self) -> list[TopicSession]:
        return await asyncio.to_thread(self.get_all_sessions)

    async def async_has_session(self, topic_id: int) -> bool:
        return await asyncio.to_thread(self.has_session, topic_id)

    async def async_add_usage(
        self,
        topic_id: int,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cost_usd: float = 0.0,
        command_type: str = "message",
    ) -> None:
        await asyncio.to_thread(
            self.add_usage,
            topic_id,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_creation_tokens,
            cost_usd,
            command_type,
        )

    async def async_mark_renamed(self, topic_id: int) -> None:
        await asyncio.to_thread(self.mark_renamed, topic_id)

    async def async_set_verbose_level(self, topic_id: int, level: int) -> None:
        await asyncio.to_thread(self.set_verbose_level, topic_id, level)

    async def async_set_subagent_tracking(self, topic_id: int, enabled: bool) -> None:
        await asyncio.to_thread(self.set_subagent_tracking, topic_id, enabled)

    async def async_set_status(self, topic_id: int, status: SessionStatus | str) -> None:
        await asyncio.to_thread(self.set_status, topic_id, status)

    async def async_get_usage(self, topic_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self.get_usage, topic_id)
