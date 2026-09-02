"""Unit-тесты единой авторизации доступа к проекту (deny-by-default).

Закрывает дыру мультиюзер-ревью: read-only/whitelist обходился, т.к.
create-сессии и slash-commands не проверяли грант, а readonly резолвился
точным сравнением строки (трейлинг-слэш → None → full).
"""
from __future__ import annotations

from pathlib import Path

from src.web.project_access import resolve_project_access


class _FakeSM:
    def __init__(self, grants: dict[int, list[dict]]):
        self._grants = grants

    def list_project_access(self, user_id: int) -> list[dict]:
        return list(self._grants.get(user_id, []))


def _mk(tmp_path: Path):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    return alpha, beta


def test_admin_gets_full_on_configured_project(tmp_path):
    alpha, beta = _mk(tmp_path)
    sm = _FakeSM({})
    acc = resolve_project_access(
        str(alpha),
        user={"user_id": 5, "is_admin": True},
        project_paths=[alpha, beta],
        whitelist=set(),
        session_manager=sm,
    )
    assert acc.allowed and acc.level == "full"


def test_whitelist_operator_gets_full(tmp_path):
    alpha, beta = _mk(tmp_path)
    sm = _FakeSM({})
    acc = resolve_project_access(
        str(beta),
        user={"user_id": 100, "is_admin": False},
        project_paths=[alpha, beta],
        whitelist={100},
        session_manager=sm,
    )
    assert acc.allowed and acc.level == "full"


def test_local_user_with_full_grant(tmp_path):
    alpha, beta = _mk(tmp_path)
    sm = _FakeSM({7: [{"project_path": str(alpha), "access_level": "full"}]})
    acc = resolve_project_access(
        str(alpha),
        user={"user_id": 7, "is_admin": False},
        project_paths=[alpha, beta],
        whitelist=set(),
        session_manager=sm,
    )
    assert acc.allowed and acc.level == "full"


def test_local_user_with_readonly_grant(tmp_path):
    alpha, beta = _mk(tmp_path)
    sm = _FakeSM({7: [{"project_path": str(alpha), "access_level": "readonly"}]})
    acc = resolve_project_access(
        str(alpha),
        user={"user_id": 7, "is_admin": False},
        project_paths=[alpha, beta],
        whitelist=set(),
        session_manager=sm,
    )
    assert acc.allowed and acc.level == "readonly"


def test_readonly_grant_not_bypassed_by_trailing_slash(tmp_path):
    """Ключевой кейс: readonly-юзер передаёт вариант своего пути с '/'.
    Точное сравнение давало None→full. Должен остаться readonly."""
    alpha, beta = _mk(tmp_path)
    sm = _FakeSM({7: [{"project_path": str(alpha), "access_level": "readonly"}]})
    acc = resolve_project_access(
        str(alpha) + "/",
        user={"user_id": 7, "is_admin": False},
        project_paths=[alpha, beta],
        whitelist=set(),
        session_manager=sm,
    )
    assert acc.allowed and acc.level == "readonly"


def test_local_user_without_grant_denied(tmp_path):
    alpha, beta = _mk(tmp_path)
    sm = _FakeSM({7: [{"project_path": str(alpha), "access_level": "full"}]})
    acc = resolve_project_access(
        str(beta),  # грант только на alpha
        user={"user_id": 7, "is_admin": False},
        project_paths=[alpha, beta],
        whitelist=set(),
        session_manager=sm,
    )
    assert not acc.allowed


def test_path_outside_configured_roots_denied_even_for_admin(tmp_path):
    alpha, beta = _mk(tmp_path)
    sm = _FakeSM({})
    acc = resolve_project_access(
        str(tmp_path),  # родитель проектов — НЕ внутри настроенных корней
        user={"user_id": 5, "is_admin": True},
        project_paths=[alpha, beta],
        whitelist=set(),
        session_manager=sm,
    )
    assert not acc.allowed


def test_no_session_manager_allows_within_roots(tmp_path):
    alpha, beta = _mk(tmp_path)
    acc = resolve_project_access(
        str(alpha),
        user={"user_id": 1, "is_admin": False},
        project_paths=[alpha, beta],
        whitelist=set(),
        session_manager=None,
    )
    assert acc.allowed and acc.level == "full"


def test_most_specific_grant_wins(tmp_path):
    """Грант readonly на под-проект строже full на родителя."""
    alpha, _ = _mk(tmp_path)
    sub = alpha / "sub"
    sub.mkdir()
    sm = _FakeSM(
        {
            7: [
                {"project_path": str(alpha), "access_level": "full"},
                {"project_path": str(sub), "access_level": "readonly"},
            ]
        }
    )
    acc = resolve_project_access(
        str(sub),
        user={"user_id": 7, "is_admin": False},
        project_paths=[alpha],
        whitelist=set(),
        session_manager=sm,
    )
    assert acc.allowed and acc.level == "readonly"
