"""H-1 / M-10: апсерт строки ``users`` для Telegram/веб-аутентифицированных.

Шеринг проектов резолвит участника по числовому id ИЛИ ``@username``, но до
этого фикса ``users.username`` писался ТОЛЬКО для локальных (логин/пароль)
аккаунтов — Telegram-login, magic-link и bot-middleware оставляли строку с
``username = NULL`` (или строки не было вовсе). Итог: и приглашение по
``@username``, и по числовому id навсегда отдавали 404. ``record_telegram_user``
чинит это, апсертя строку на каждом аутентифицированном взаимодействии.
"""
from __future__ import annotations

import os
import tempfile

from src.claude.session import SessionManager


def _new_manager() -> tuple[SessionManager, str]:
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


def test_creates_row_with_username() -> None:
    """Нового Telegram-юзера с @username находим И по id, И по username."""
    sm, path = _new_manager()
    try:
        sm.record_telegram_user(555, "colleague")
        assert sm.get_user_by_id(555) is not None
        found = sm.get_user_by_username("colleague")
        assert found is not None and found["user_id"] == 555
        assert found["origin"] == "telegram"
    finally:
        _cleanup(sm, path)


def test_creates_row_without_username() -> None:
    """M-10: magic-link не несёт username → строка всё равно создаётся, чтобы
    приглашение по числовому id работало."""
    sm, path = _new_manager()
    try:
        sm.record_telegram_user(777, None)
        row = sm.get_user_by_id(777)
        assert row is not None
        assert row["username"] is None
    finally:
        _cleanup(sm, path)


def test_is_idempotent() -> None:
    """Повторные вызовы не падают и не плодят строк."""
    sm, path = _new_manager()
    try:
        sm.record_telegram_user(1, "a")
        sm.record_telegram_user(1, "a")
        sm.record_telegram_user(1, "a")
        assert sm.get_user_by_id(1) is not None
        assert sm.list_users() and len(sm.list_users()) == 1
    finally:
        _cleanup(sm, path)


def test_backfills_username_on_later_call() -> None:
    """Строка без username (создана magic-link) получает username, когда юзер
    позже пишет боту (username уже доступен)."""
    sm, path = _new_manager()
    try:
        sm.record_telegram_user(42, None)
        assert sm.get_user_by_username("late") is None
        sm.record_telegram_user(42, "late")
        found = sm.get_user_by_username("late")
        assert found is not None and found["user_id"] == 42
    finally:
        _cleanup(sm, path)


def test_updates_username_when_changed() -> None:
    """Смена @username в Telegram отражается в БД (перепривязка к тому же id)."""
    sm, path = _new_manager()
    try:
        sm.record_telegram_user(9, "old_handle")
        sm.record_telegram_user(9, "new_handle")
        assert sm.get_user_by_username("new_handle")["user_id"] == 9
        # Старый handle больше не резолвится в этого юзера.
        assert sm.get_user_by_username("old_handle") is None
    finally:
        _cleanup(sm, path)


def test_empty_username_does_not_clobber_existing() -> None:
    """Пустой/None username НЕ затирает уже записанный (magic-link после того,
    как username уже был известен из бота)."""
    sm, path = _new_manager()
    try:
        sm.record_telegram_user(3, "keep")
        sm.record_telegram_user(3, "")
        sm.record_telegram_user(3, None)
        found = sm.get_user_by_username("keep")
        assert found is not None and found["user_id"] == 3
    finally:
        _cleanup(sm, path)


def test_does_not_clobber_local_account_username() -> None:
    """Локальный (логин/пароль) аккаунт не перетирается telegram-апсертом того
    же id (маловероятная коллизия id, но фикс безопасен)."""
    sm, path = _new_manager()
    try:
        uid = sm.create_local_user(
            username="teacher", password_hash="x", is_admin=True
        )
        sm.record_telegram_user(uid, "hacker_handle")
        row = sm.get_user_by_id(uid)
        assert row["username"] == "teacher"
        assert row["origin"] == "local"
    finally:
        _cleanup(sm, path)


def test_username_collision_keeps_row_skips_rename() -> None:
    """Если @username уже занят другим id (username переиспользован в Telegram),
    строку второго юзера НЕ теряем — просто не присваиваем занятый username
    (partial-unique-индекс не нарушается)."""
    sm, path = _new_manager()
    try:
        sm.record_telegram_user(100, "shared")
        sm.record_telegram_user(200, "shared")  # 100 ещё держит "shared"
        # Второй юзер существует (строка есть), но username к нему не привязался.
        assert sm.get_user_by_id(200) is not None
        assert sm.get_user_by_username("shared")["user_id"] == 100
    finally:
        _cleanup(sm, path)
