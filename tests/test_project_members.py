"""Store-слой самообслуживаемого шеринга проектов: granted_by + участники.

Проверяет миграцию колонки ``granted_by`` в ``user_project_access`` и новые
store-методы (``get_project_access_row`` / ``list_project_members`` /
``get_project_abspath``). Каждый тест поднимает SessionManager на свежем
temp-файле БД (mkstemp) и закрывает/удаляет его в finally.
"""
from __future__ import annotations

import os
import tempfile

from src.claude.session import SessionManager


def _new_manager() -> tuple[SessionManager, str]:
    """SessionManager на свежем temp-файле БД. Вызывающий обязан _cleanup."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return SessionManager(storage_path=path), path


def _cleanup(sm: SessionManager, path: str) -> None:
    sm.close_sync()
    sm._engine.sync_engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _add_user(sm: SessionManager, user_id: int) -> None:
    """``user_project_access.user_id`` имеет FK на ``users`` (foreign_keys=ON),
    поэтому строка юзера обязана существовать до выдачи доступа. Апсертим
    telegram-строку — ``username`` при этом остаётся NULL."""
    sm.set_user_verbose(user_id, 1)


def test_granted_by_column_exists() -> None:
    sm, path = _new_manager()
    try:
        with sm._conn_lock:
            conn = sm._get_connection()
            cols = [
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(user_project_access)"
                ).fetchall()
            ]
        assert "granted_by" in cols
    finally:
        _cleanup(sm, path)


def test_set_project_access_stores_granted_by() -> None:
    sm, path = _new_manager()
    try:
        _add_user(sm, 10)
        sm.set_project_access(10, "/srv/p1", "full", granted_by=99)
        # Строка выдана под путём без проекта → project_id NULL, матч по пути.
        assert sm.get_project_access_row(10, 0, "/srv/p1") == {
            "access_level": "full",
            "granted_by": 99,
        }
    finally:
        _cleanup(sm, path)


def test_set_project_access_default_granted_by_none() -> None:
    sm, path = _new_manager()
    try:
        _add_user(sm, 11)
        sm.set_project_access(11, "/srv/p1", "readonly")
        assert sm.get_project_access_row(11, 0, "/srv/p1") == {
            "access_level": "readonly",
            "granted_by": None,
        }
    finally:
        _cleanup(sm, path)


def test_get_project_access_row_missing_returns_none() -> None:
    sm, path = _new_manager()
    try:
        assert sm.get_project_access_row(123, 0, "/nope") is None
    finally:
        _cleanup(sm, path)


def test_list_project_members() -> None:
    sm, path = _new_manager()
    try:
        for uid in (1, 2, 3):
            _add_user(sm, uid)
        # /srv/p1: два участника — admin-грант (granted_by=None) и self-service.
        sm.set_project_access(1, "/srv/p1", "full")
        sm.set_project_access(2, "/srv/p1", "readonly", granted_by=10)
        # /srv/p2: посторонний участник — не должен попасть в выборку p1.
        sm.set_project_access(3, "/srv/p2", "full", granted_by=10)

        # project_id=0 не совпадает ни с чем → матч только по пути /srv/p1.
        members = sm.list_project_members(0, "/srv/p1")
        assert len(members) == 2
        by_uid = {m["user_id"]: m for m in members}
        assert by_uid[1]["access_level"] == "full"
        assert by_uid[1]["granted_by"] is None
        assert by_uid[2]["access_level"] == "readonly"
        assert by_uid[2]["granted_by"] == 10
    finally:
        _cleanup(sm, path)


def _insert_grant_raw(
    sm: SessionManager, user_id: int, project_path: str,
    access_level: str, project_id: int, granted_by: int | None,
) -> None:
    """Вставляет строку доступа НАПРЯМУЮ (обходя set_project_access), чтобы
    смоделировать грант, лежащий под НЕ-каноническим ``project_path``, но с
    верным ``project_id`` (легаси/рассинхрон пути)."""
    with sm._conn_lock:
        conn = sm._get_connection()
        conn.execute(
            "INSERT INTO user_project_access "
            "(user_id, project_path, access_level, project_id, granted_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, project_path, access_level, project_id, granted_by),
        )
        conn.commit()


def test_membership_matches_by_project_id_under_noncanonical_path() -> None:
    # F3: грант под не-каноническим путём, но с правильным project_id, виден
    # и по list_project_members(project_id, canonical), и по get_project_access_row.
    sm, path = _new_manager()
    try:
        _add_user(sm, 20)
        pid = sm.create_project("/srv/canonical")["id"]
        _insert_grant_raw(sm, 20, "/srv/canonical/", "full", pid, granted_by=7)

        # По каноническому пути точного совпадения нет → находим по project_id.
        members = sm.list_project_members(pid, "/srv/canonical")
        assert len(members) == 1
        assert members[0]["user_id"] == 20
        assert members[0]["access_level"] == "full"

        assert sm.get_project_access_row(20, pid, "/srv/canonical") == {
            "access_level": "full",
            "granted_by": 7,
        }
    finally:
        _cleanup(sm, path)


def test_revoke_for_project_removes_noncanonical_row() -> None:
    # F3: revoke_project_access_for_project снимает грант даже если он лежит под
    # не-каноническим путём (матч по project_id ИЛИ точному пути).
    sm, path = _new_manager()
    try:
        _add_user(sm, 21)
        pid = sm.create_project("/srv/canonical2")["id"]
        _insert_grant_raw(sm, 21, "/srv/canonical2/", "readonly", pid, granted_by=7)

        sm.revoke_project_access_for_project(21, pid, "/srv/canonical2")

        assert sm.get_project_access_row(21, pid, "/srv/canonical2") is None
        assert sm.list_project_members(pid, "/srv/canonical2") == []
    finally:
        _cleanup(sm, path)
