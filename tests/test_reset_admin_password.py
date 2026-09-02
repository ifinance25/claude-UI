"""Тесты логики сброса пароля веб-админа (scripts/reset-admin-password.sh).

Скрипт восстанавливает доступ к веб-админке, если оператор потерял пароль
``ADMIN_LOGIN`` (показывается один раз при установке). Пароль хранится как
PBKDF2-хэш и расшифровать его нельзя, поэтому скрипт ЗАДАЁТ НОВЫЙ.

Здесь прогоняем САМУ логику сброса в процессе (не через shell): реальный
``SessionManager`` на временной БД → ``create_local_user(admin)`` → имитируем
шаги скрипта (``get_user_by_username`` + ``set_user_password(hash_password(new))``)
→ проверяем, что новый пароль верифицируется, а старый — нет. Плюс ``bash -n``
синтаксис-чек самого скрипта.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from src.web.passwords import hash_password, verify_password

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "reset-admin-password.sh"


def test_reset_admin_password_updates_stored_hash(make_mgr):
    """Основной сценарий: старый пароль перестаёт подходить, новый — подходит."""
    sm = make_mgr()
    sm.create_local_user(
        username="admin", password_hash=hash_password("old"), is_admin=True
    )

    # ── старый пароль действителен до сброса ─────────────────────────
    u = sm.get_user_by_username("admin")
    assert u is not None
    assert verify_password("old", u["password_hash"])

    # ── имитация шагов скрипта: найти админа и переписать хэш ─────────
    ok = sm.set_user_password(u["user_id"], hash_password("new"))
    assert ok is True

    # ── после сброса: новый подходит, старый — нет ───────────────────
    u2 = sm.get_user_by_username("admin")
    assert u2 is not None
    assert verify_password("new", u2["password_hash"])
    assert not verify_password("old", u2["password_hash"])


def test_reset_bumps_token_version_to_revoke_sessions(make_mgr):
    """Смена пароля инкрементит token_version → старые JWT отзываются (H-3)."""
    sm = make_mgr()
    sm.create_local_user(
        username="admin", password_hash=hash_password("old"), is_admin=True
    )
    before = sm.get_user_by_username("admin")["token_version"]
    sm.set_user_password(
        sm.get_user_by_username("admin")["user_id"], hash_password("new")
    )
    after = sm.get_user_by_username("admin")["token_version"]
    assert after == before + 1


def test_reset_missing_admin_is_none(make_mgr):
    """Нет локального админа → get_user_by_username вернёт None (скрипт → exit 1)."""
    sm = make_mgr()
    assert sm.get_user_by_username("admin") is None


def test_reset_script_syntax_ok():
    """`bash -n` должен пройти на самом скрипте."""
    assert _SCRIPT.exists(), f"script missing: {_SCRIPT}"
    res = subprocess.run(
        ["bash", "-n", str(_SCRIPT)], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
