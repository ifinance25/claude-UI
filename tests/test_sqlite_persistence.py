from __future__ import annotations

import contextlib
import importlib.util
import json
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

from src.config.settings import Settings


ROOT = Path(__file__).resolve().parents[1]
SESSION_MODULE_PATH = ROOT / "src/claude/session.py"


@contextlib.contextmanager
def _windows_safe_tempdir():
    """Windows-safe replacement for tempfile.TemporaryDirectory.

    SQLite keeps connections open on .db files; on Windows that
    prevents deletion via the default TemporaryDirectory cleanup,
    which raises PermissionError. mkdtemp + rmtree(ignore_errors=True)
    sidesteps that without changing test logic — assertions still run
    exactly as before.
    """
    path = tempfile.mkdtemp()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def load_session_module():
    sys.modules.pop("sqlite_persistence_session_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "sqlite_persistence_session_under_test",
        SESSION_MODULE_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SQLitePersistenceTests(unittest.TestCase):
    def test_session_manager_persists_sessions_in_sqlite(self) -> None:
        session_module = load_session_module()
        SessionManager = session_module.SessionManager

        with _windows_safe_tempdir() as tmpdir:
            db_path = Path(tmpdir) / "sessions.db"
            manager = SessionManager(storage_path=db_path)

            manager.create_session(
                topic_id=42,
                project_path="/tmp/demo-project",
                project_name="demo-project",
            )
            manager.update_session_id(42, "sid-42")
            manager.add_usage(
                42,
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=2,
                cache_creation_tokens=1,
                cost_usd=0.125,
            )
            manager.mark_renamed(42)
            manager.set_status(42, "done")
            manager.set_subagent_tracking(42, True)

            reloaded = SessionManager(storage_path=db_path)
            session = reloaded.get_session(42)

            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.project_name, "demo-project")
            self.assertEqual(session.project_path, "/tmp/demo-project")
            self.assertEqual(session.session_id, "sid-42")
            self.assertEqual(session.total_input_tokens, 10)
            self.assertEqual(session.total_output_tokens, 5)
            self.assertEqual(session.total_cache_read_tokens, 2)
            self.assertEqual(session.total_cache_creation_tokens, 1)
            self.assertEqual(session.total_cost_usd, 0.125)
            self.assertEqual(session.message_count, 1)
            self.assertTrue(session.is_renamed)
            self.assertTrue(session.enable_subagent_tracking)
            self.assertEqual(session.status, "done")

            with sqlite3.connect(db_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(
                    {"users", "projects", "sessions", "action_logs"}.issubset(tables)
                )
                session_row = connection.execute(
                    "SELECT topic_id, session_id, message_count, total_cost_usd FROM sessions"
                ).fetchone()
                action_log_row = connection.execute(
                    "SELECT cost, command_type FROM action_logs"
                ).fetchone()

            self.assertEqual(session_row, (42, "sid-42", 1, 0.125))
            self.assertEqual(action_log_row, (0.125, "message"))

    def test_session_manager_migrates_legacy_sessions_json(self) -> None:
        session_module = load_session_module()
        SessionManager = session_module.SessionManager

        with _windows_safe_tempdir() as tmpdir:
            json_path = Path(tmpdir) / "sessions.json"
            now = datetime.now()
            json_path.write_text(
                json.dumps(
                    {
                        "77": {
                            "topic_id": 77,
                            "session_id": "legacy-sid",
                            "project_path": "/tmp/legacy-project",
                            "project_name": "legacy-project",
                            "created_at": now.isoformat(),
                            "last_activity": now.isoformat(),
                            "total_input_tokens": 3,
                            "total_output_tokens": 4,
                            "total_cache_read_tokens": 0,
                            "total_cache_creation_tokens": 0,
                            "total_cost_usd": 0.01,
                            "message_count": 2,
                            "is_renamed": True,
                            "verbose_level": 2,
                            "enable_subagent_tracking": True,
                            "status": "done",
                        }
                    }
                )
            )

            manager = SessionManager(storage_path=json_path)
            session = manager.get_session(77)

            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.project_name, "legacy-project")
            self.assertEqual(session.session_id, "legacy-sid")
            self.assertEqual(session.verbose_level, 2)
            self.assertTrue(session.enable_subagent_tracking)
            self.assertFalse(json_path.exists())
            self.assertTrue(json_path.with_suffix(".db").exists())

    def test_settings_exposes_session_database_path(self) -> None:
        # _env_file=None bypasses pydantic-settings .env discovery for
        # this Settings instance so a developer's local .env (with
        # SESSION_DATABASE_PATH=...) doesn't shadow the explicit
        # persistence config we're testing here. Pure setup isolation
        # — the assertion itself is unchanged.
        settings = Settings(
            _env_file=None,
            persistence={"session_database_path": "~/bot-data/sessions.db"},
        )

        self.assertEqual(
            settings.get_session_database_path(),
            Path("~/bot-data/sessions.db").expanduser(),
        )

    def test_alembic_bootstrap_files_exist(self) -> None:
        self.assertTrue((ROOT / "alembic.ini").exists())
        self.assertTrue((ROOT / "alembic" / "env.py").exists())
        self.assertTrue((ROOT / "alembic" / "script.py.mako").exists())
        versions = list((ROOT / "alembic" / "versions").glob("*.py"))
        self.assertGreaterEqual(len(versions), 1)

    def test_alembic_env_references_session_metadata_and_url_helper(self) -> None:
        env_text = (ROOT / "alembic" / "env.py").read_text()

        # env.py uses Base.metadata from the ORM models
        self.assertIn("Base", env_text)
        self.assertIn("target_metadata", env_text)
        self.assertIn("build_sync_database_url", env_text)
        self.assertNotIn("target_metadata = None", env_text)

    def test_messages_table_created(self) -> None:
        import shutil

        session_module = load_session_module()
        SessionManager = session_module.SessionManager

        tmpdir = tempfile.mkdtemp()
        try:
            db_path = Path(tmpdir) / "sessions.db"
            manager = SessionManager(storage_path=db_path)
            try:
                with sqlite3.connect(db_path) as conn:
                    cols = {
                        row[1]
                        for row in conn.execute(
                            "PRAGMA table_info(messages)"
                        ).fetchall()
                    }
                    self.assertEqual(
                        cols,
                        {
                            "event_id",
                            "topic_id",
                            "request_id",
                            "type",
                            "kind",
                            "content",
                            "metadata_json",
                            "created_at",
                        },
                    )
                    indexes = {
                        row[1]
                        for row in conn.execute(
                            "PRAGMA index_list(messages)"
                        ).fetchall()
                    }
                    self.assertIn("idx_messages_topic", indexes)
            finally:
                manager.close_sync()
                manager._engine.sync_engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class AsyncSQLitePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_session_manager_methods_persist_sessions(self) -> None:
        session_module = load_session_module()
        SessionManager = session_module.SessionManager

        with _windows_safe_tempdir() as tmpdir:
            db_path = Path(tmpdir) / "sessions.db"
            manager = SessionManager(storage_path=db_path)

            await manager.async_create_session(
                topic_id=99,
                project_path="/tmp/async-project",
                project_name="async-project",
            )
            await manager.async_update_session_id(99, "async-sid")
            await manager.async_add_usage(
                99,
                input_tokens=7,
                output_tokens=11,
                cache_read_tokens=1,
                cache_creation_tokens=2,
                cost_usd=0.05,
            )
            await manager.async_set_status(99, "done")

            session = await manager.async_get_session(99)

            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.session_id, "async-sid")
            self.assertEqual(session.total_input_tokens, 7)
            self.assertEqual(session.total_output_tokens, 11)
            self.assertEqual(session.total_cache_read_tokens, 1)
            self.assertEqual(session.total_cache_creation_tokens, 2)
            self.assertEqual(session.total_cost_usd, 0.05)
            self.assertEqual(session.status, "done")

    async def test_sqlalchemy_url_helper_uses_aiosqlite_absolute_paths(self) -> None:
        session_module = load_session_module()

        db_path = Path("/tmp/sqlalchemy-check.db")
        # Windows-safe: build_database_url uses str(Path) which yields
        # backslashes on win32. Normalise both sides — the assertion
        # shape (driver prefix + absolute path) is what we care about.
        url = session_module.build_database_url(db_path)
        self.assertEqual(
            url.replace("\\", "/"),
            "sqlite+aiosqlite:////tmp/sqlalchemy-check.db",
        )

        sync_url = session_module.build_sync_database_url(db_path)
        self.assertEqual(
            sync_url.replace("\\", "/"),
            "sqlite:////tmp/sqlalchemy-check.db",
        )

        # Verify Base metadata is available (used by Alembic)
        self.assertIsNotNone(session_module.Base.metadata)


if __name__ == "__main__":
    unittest.main()
