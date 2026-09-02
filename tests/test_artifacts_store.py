"""Юнит-тесты SessionManager: tool-artifact rows + dismissals."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from src.claude.session import SessionManager


@pytest.fixture
def sm():
    tmp = Path(tempfile.mkdtemp())
    manager = SessionManager(storage_path=tmp / "sessions.db")
    try:
        yield manager
    finally:
        manager.close_sync()
        manager._engine.sync_engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def _insert_msg(sm, topic_id, kind, metadata, created_at="2026-01-01 00:00:00"):
    conn = sm._get_connection()
    conn.execute(
        "INSERT INTO messages (topic_id, type, kind, content, metadata_json, created_at) "
        "VALUES (?, 'streaming_update', ?, '', ?, ?)",
        (topic_id, kind, json.dumps(metadata) if metadata is not None else None, created_at),
    )
    conn.commit()


def test_list_tool_artifact_rows_filters(sm):
    sm.create_session(topic_id=-1, project_path="/p", project_name="p", chat_id=1)
    _insert_msg(sm, -1, "tool_use", {"name": "Write", "file_path": "/p/a.txt"}, "2026-01-01 00:00:01")
    _insert_msg(sm, -1, "tool_use", {"name": "Edit", "file_path": "/p/a.txt"}, "2026-01-01 00:00:02")
    _insert_msg(sm, -1, "tool_use", {"name": "Bash"}, "2026-01-01 00:00:03")  # нет file_path
    _insert_msg(sm, -1, "text", None, "2026-01-01 00:00:04")  # не tool_use
    rows = sm.list_tool_artifact_rows()
    assert len(rows) == 2
    assert {r["tool"] for r in rows} == {"Write", "Edit"}
    assert all(r["file_path"] == "/p/a.txt" for r in rows)
    assert rows[0]["created_at"] == "2026-01-01 00:00:01"


def test_list_tool_artifact_rows_skips_bad_json(sm):
    sm.create_session(topic_id=-2, project_path="/p", project_name="p", chat_id=1)
    conn = sm._get_connection()
    conn.execute(
        "INSERT INTO messages (topic_id, type, kind, content, metadata_json, created_at) "
        "VALUES (?, 'streaming_update', 'tool_use', '', ?, ?)",
        (-2, '{"file_path": broken', "2026-01-01 00:00:05"),
    )
    conn.commit()
    assert sm.list_tool_artifact_rows() == []  # битый JSON тихо пропущен


def test_dismissal_roundtrip_and_idempotent(sm):
    sm.add_artifact_dismissal(7, "/root", "/root/x.txt")
    sm.add_artifact_dismissal(7, "/root", "/root/x.txt")  # INSERT OR IGNORE
    assert sm.list_artifact_dismissals(7, "/root") == ["/root/x.txt"]
    sm.remove_artifact_dismissal(7, "/root", "/root/x.txt")
    assert sm.list_artifact_dismissals(7, "/root") == []


def test_dismissal_per_user_and_per_project(sm):
    sm.add_artifact_dismissal(1, "/root", "/root/x.txt")
    sm.add_artifact_dismissal(2, "/root", "/root/y.txt")
    sm.add_artifact_dismissal(1, "/other", "/other/z.txt")
    assert sm.list_artifact_dismissals(1, "/root") == ["/root/x.txt"]
    assert sm.list_artifact_dismissals(2, "/root") == ["/root/y.txt"]
    assert sm.list_artifact_dismissals(1, "/other") == ["/other/z.txt"]


def test_messages_kind_index_exists(sm):
    conn = sm._get_connection()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_messages_kind" in names


def test_delete_project_purges_artifact_dismissals(sm):
    pid = sm.create_project("/srv/projX")["id"]
    # Ключ группировки скрытий — resolved-путь корня; чистим оба варианта.
    sm.add_artifact_dismissal(5, "/srv/projX", "/srv/projX/a.txt")
    sm.add_artifact_dismissal(5, str(Path("/srv/projX").resolve()), "/srv/projX/b.txt")
    assert sm.delete_project(pid) is True
    assert sm.list_artifact_dismissals(5, "/srv/projX") == []
    assert sm.list_artifact_dismissals(5, str(Path("/srv/projX").resolve())) == []


def test_rename_project_purges_old_artifact_dismissals(sm):
    pid = sm.create_project("/srv/projY")["id"]
    sm.add_artifact_dismissal(6, "/srv/projY", "/srv/projY/a.txt")
    sm.add_artifact_dismissal(6, str(Path("/srv/projY").resolve()), "/srv/projY/b.txt")
    sm.rename_project(pid, "/srv/projY-renamed")
    # Старые скрытия (rel вёл на старый путь) вычищены — мёртвых строк нет.
    assert sm.list_artifact_dismissals(6, "/srv/projY") == []
    assert sm.list_artifact_dismissals(6, str(Path("/srv/projY").resolve())) == []
