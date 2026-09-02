"""Тесты гейта самообслуживаемого шеринга проектов (Task 3).

Покрывает:
  * ``can_manage_grant`` — чистое правило «кто может трогать грант»
    (админ — любой; не-админ — только свой ``granted_by == actor``;
    админский/системный ``granted_by is None`` — неприкосновенен);
  * ``require_project_manage_factory`` — DI-гейт «управлять участниками»
    (full-доступ по path-параметру ``project_id`` ИЛИ админ).

``require_project_manage`` тестируется через открытый хук
``.__wrapped_check__(project_id, user)`` — так не нужен FastAPI DI. Гейт
(F2) авторизует не-админа ТОЛЬКО по ЯВНОМУ full-гранту на ЭТОТ проект
(``session_manager.get_project_access_row``), а не по whitelist-implicit
``resolve_project_access``. ``asyncio_mode = "auto"`` (pyproject) → async-тесты
исполняются без явных маркеров.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.web.dependencies import (
    can_manage_grant,
    require_project_manage_factory,
)

# --------------------------------------------------------------------------- #
# can_manage_grant — чистое правило
# --------------------------------------------------------------------------- #


def test_admin_manages_system_grant():
    # Админ может трогать даже системный/админский грант (granted_by is None).
    assert can_manage_grant(actor_id=5, is_admin=True, granted_by=None) is True


def test_admin_manages_other_users_grant():
    assert can_manage_grant(actor_id=5, is_admin=True, granted_by=999) is True


def test_non_admin_manages_own_grant():
    assert can_manage_grant(actor_id=7, is_admin=False, granted_by=7) is True


def test_non_admin_cannot_manage_others_grant():
    assert can_manage_grant(actor_id=7, is_admin=False, granted_by=8) is False


def test_non_admin_cannot_manage_system_grant():
    # granted_by is None → системный/админский → неприкосновенен для не-админа.
    assert can_manage_grant(actor_id=7, is_admin=False, granted_by=None) is False


# --------------------------------------------------------------------------- #
# require_project_manage_factory — DI-гейт
# --------------------------------------------------------------------------- #


def _make_sm(abspath, user_row=None, access_row=None):
    sm = MagicMock()
    sm.get_project_abspath.return_value = abspath
    sm.get_user_by_id.return_value = user_row
    sm.get_project_access_row.return_value = access_row
    return sm


def _make_dep(sm):
    return require_project_manage_factory(
        "secret", session_manager=sm, allowed_user_ids=[100],
    )


async def test_explicit_full_grant_non_admin_allowed():
    # Не-админ с ЯВНЫМ full-грантом на этот проект → доступ (не whitelist-implicit).
    sm = _make_sm(
        "/srv/projects/alpha",
        user_row={"is_admin": 0},
        access_row={"access_level": "full", "granted_by": 7},
    )
    ctx = await _make_dep(sm).__wrapped_check__(1, {"user_id": 42, "is_admin": False})
    assert ctx["project_path"] == "/srv/projects/alpha"
    assert ctx["project_id"] == 1
    assert ctx["is_admin"] is False
    assert ctx["user"]["user_id"] == 42


async def test_readonly_grant_non_admin_forbidden():
    # Явный грант есть, но readonly → управлять участниками нельзя.
    sm = _make_sm(
        "/srv/projects/alpha",
        user_row={"is_admin": 0},
        access_row={"access_level": "readonly", "granted_by": 7},
    )
    with pytest.raises(HTTPException) as exc:
        await _make_dep(sm).__wrapped_check__(1, {"user_id": 42, "is_admin": False})
    assert exc.value.status_code == 403


async def test_no_grant_non_admin_forbidden():
    # Нет строки гранта (включая whitelist-оператора без явного гранта) → 403.
    sm = _make_sm(
        "/srv/projects/alpha", user_row={"is_admin": 0}, access_row=None
    )
    with pytest.raises(HTTPException) as exc:
        await _make_dep(sm).__wrapped_check__(1, {"user_id": 42, "is_admin": False})
    assert exc.value.status_code == 403


async def test_missing_user_id_forbidden():
    # Нет user_id → нельзя резолвить грант → 403.
    sm = _make_sm(
        "/srv/projects/alpha",
        user_row=None,
        access_row={"access_level": "full", "granted_by": None},
    )
    with pytest.raises(HTTPException) as exc:
        await _make_dep(sm).__wrapped_check__(1, {"is_admin": False})
    assert exc.value.status_code == 403


async def test_missing_project_404():
    sm = _make_sm(None, user_row={"is_admin": 0})
    with pytest.raises(HTTPException) as exc:
        await _make_dep(sm).__wrapped_check__(999, {"user_id": 42, "is_admin": False})
    assert exc.value.status_code == 404


async def test_storage_unavailable_503():
    dep = require_project_manage_factory(
        "secret", session_manager=None, allowed_user_ids=[],
    )
    with pytest.raises(HTTPException) as exc:
        await dep.__wrapped_check__(1, {"user_id": 42, "is_admin": False})
    assert exc.value.status_code == 503


async def test_admin_allowed_regardless_of_grant():
    # DB-авторитетный is_admin=1 → доступ есть даже без full-гранта (grant None).
    sm = _make_sm(
        "/srv/projects/alpha", user_row={"is_admin": 1}, access_row=None
    )
    ctx = await _make_dep(sm).__wrapped_check__(1, {"user_id": 42, "is_admin": False})
    assert ctx["is_admin"] is True
    assert ctx["project_id"] == 1
    assert ctx["project_path"] == "/srv/projects/alpha"
