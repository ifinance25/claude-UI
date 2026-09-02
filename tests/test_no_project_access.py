"""C-1: сессия без проекта («чистый Claude») даёт ПОЛНЫЙ доступ только
доверенным операторам (admin / whitelist). Для локальных/readonly-юзеров она
запускается readonly — иначе обходит всю модель project-access (Bash/Write в
любой каталог = RCE).
"""
from src.web.routes_ws import no_project_readonly


def test_whitelist_user_gets_full() -> None:
    assert no_project_readonly(user_id=777, whitelist={777, 888}, db_is_admin=False) is False


def test_admin_gets_full() -> None:
    assert no_project_readonly(user_id=1_000_000_005, whitelist=set(), db_is_admin=True) is False


def test_local_non_admin_is_readonly() -> None:
    """Корень C-1: локальный readonly/без-грантов юзер → readonly в no-project."""
    assert no_project_readonly(user_id=1_000_000_005, whitelist={777}, db_is_admin=False) is True


def test_local_non_admin_empty_whitelist_is_readonly() -> None:
    assert no_project_readonly(user_id=1_000_000_005, whitelist=None, db_is_admin=False) is True
