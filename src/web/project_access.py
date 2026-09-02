"""Единая авторизация доступа пользователя к проекту (deny-by-default).

Раньше проверка «можно ли юзеру работать с этим project_path» жила в трёх
расходящихся видах: фильтр в routes_projects, _validate_project в routes_docs,
и ВОВСЕ отсутствовала в routes_sessions (создание сессии), routes_ws (выбор
readonly) и routes_model (slash-commands). Из-за этого read-only/whitelist
обходился: создать сессию/слэш-команду можно было на любом пути сервера, а
readonly резолвился точным сравнением строки (трейлинг-слэш → None → full).

Один источник правды. Семантика deny-by-default:
  * путь обязан лежать внутри одного из настроенных корней проектов;
  * админ и Telegram-операторы (whitelist) — full на любой настроенный проект;
  * режим без БД (session_manager is None) — full (тесты/legacy single-user);
  * локальный пользователь — только явно выданные гранты, уровень из гранта;
    сопоставление по нормализованному (resolved) пути, поэтому трейлинг-слэш,
    под-каталоги и симлинк-варианты не обходят грант и не теряют уровень.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.utils.url_safety import is_path_within_root


@dataclass(frozen=True)
class ProjectAccess:
    """Решение об авторизации. ``level`` валиден только при ``allowed``."""

    allowed: bool
    level: str  # "full" | "readonly"
    root: Path | None


_DENIED = ProjectAccess(False, "full", None)


def _safe_resolve(path: str | Path) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return None


def resolve_project_access(
    project_path: str,
    *,
    user: dict[str, Any],
    project_paths: list,
    whitelist,
    session_manager: Any,
) -> ProjectAccess:
    """Авторизует доступ ``user`` к ``project_path``; см. модульный docstring."""
    root = _safe_resolve(project_path)
    if root is None:
        return _DENIED

    # 1) Путь обязан лежать внутри одного из настроенных корней проектов.
    roots = [r for r in (_safe_resolve(p) for p in project_paths) if r is not None]
    if not any(is_path_within_root(r, root) for r in roots):
        return _DENIED

    # 2) Режим без БД / админ / Telegram-оператор — полный доступ.
    if session_manager is None:
        return ProjectAccess(True, "full", root)
    if user.get("is_admin") or int(user["user_id"]) in set(whitelist or ()):
        return ProjectAccess(True, "full", root)

    # 3) Локальный пользователь — только явно выданные проекты. Совпадение по
    #    нормализованному пути; при нескольких грантах выигрывает самый
    #    специфичный (longest-prefix), чтобы readonly на под-проект не
    #    «расширялся» до full родителя.
    granted = session_manager.list_project_access(int(user["user_id"]))
    best_level: str | None = None
    best_len = -1
    for g in granted:
        groot = _safe_resolve(g.get("project_path", ""))
        if groot is None or not is_path_within_root(groot, root):
            continue
        glen = len(str(groot))
        if glen > best_len:
            best_len = glen
            level = g.get("access_level") or "full"
            best_level = "readonly" if level == "readonly" else "full"
    if best_level is None:
        return _DENIED
    return ProjectAccess(True, best_level, root)
