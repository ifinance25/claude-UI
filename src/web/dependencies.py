"""FastAPI DI helpers."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Cookie, Depends, HTTPException, status

from src.web.auth import decode_jwt

# Локальные аккаунты (создаются в админке) живут в диапазоне id ≥ этого
# порога (см. SessionManager.create_local_user). Telegram/whitelist-юзеры —
# ниже. Для локальных перепроверяем БД на каждом запросе, чтобы «Отключить»/
# «Удалить» в админке отзывали активную сессию немедленно, а не через TTL JWT.
LOCAL_USER_ID_MIN = 1_000_000_001


async def is_account_active(
    session_manager, user: dict[str, Any], allowed_user_ids=None
) -> bool:
    """Активен ли аккаунт в данный момент (для немедленного отзыва доступа).

    Тип аккаунта определяем по полю ``users.origin`` (H-2), а НЕ по диапазону
    id: современные Telegram user_id бывают БОЛЬШЕ ``LOCAL_USER_ID_MIN`` и при
    старой классификации (порог id) Telegram-юзер со строкой в ``users`` (он
    появляется, как только юзер хоть раз менял verbose) ошибочно считался
    локальным и НЕ отзывался при снятии из whitelist.

    Правила:
    - ``session_manager is None`` (тесты/legacy single-user): активен.
    - DB-deny приоритетнее whitelist-allow: если в ``users`` есть строка и
      ``is_active = 0`` → отзыв (H-4: деактивация оператора в админке).
    - ЛОКАЛЬНЫЙ аккаунт (``origin='local'``): существование + ``is_active`` +
      совпадение ``token_version`` с JWT-claim ``tv`` (H-3: смена пароля
      инкрементит версию → старые токены отвергаются).
    - Telegram-юзер (``origin='telegram'`` / нет строки): при настроенном
      whitelist — активен ⇔ он в whitelist (немедленный отзыв при снятии);
      без whitelist (legacy) — активен.
    """
    try:
        uid = int(user.get("user_id"))
    except (TypeError, ValueError):
        return True

    row = None
    if session_manager is not None:
        row = await asyncio.to_thread(session_manager.get_user_by_id, uid)
    # DB-deny имеет приоритет над whitelist-allow.
    if row is not None and not row.get("is_active", 1):
        return False

    origin = (row or {}).get("origin")
    # Локальный аккаунт: либо явно origin='local', либо (для очень старых строк
    # без origin) определяем по наличию password_hash.
    is_local = origin == "local" or (
        origin is None and bool((row or {}).get("password_hash"))
    )
    if is_local:
        if row is None:
            return False
        claim_tv = user.get("tv")
        if claim_tv is not None and int(claim_tv) != int(row.get("token_version") or 0):
            return False
        return True

    # Telegram/whitelist-аккаунт.
    if allowed_user_ids:
        return uid in set(allowed_user_ids)
    return True


def get_current_user_factory(jwt_secret: str, session_manager=None, allowed_user_ids=None):
    """Create a FastAPI dependency that returns the current user dict or 401.

    Когда передан ``session_manager``, локальные аккаунты дополнительно
    перепроверяются на существование + is_active. Когда передан непустой
    ``allowed_user_ids`` — Telegram/whitelist-юзеры перепроверяются на наличие
    в живом whitelist (немедленный отзыв при снятии из ALLOWED_USER_IDS)."""

    async def get_current_user(
        vels_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        if not vels_session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="no session"
            )
        try:
            user = decode_jwt(vels_session, secret=jwt_secret)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid session"
            )
        if not await is_account_active(session_manager, user, allowed_user_ids):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="account disabled or removed",
            )
        return user

    return get_current_user


def require_admin_factory(jwt_secret: str, session_manager=None, allowed_user_ids=None):
    """Dependency: пропускает только пользователей с ролью admin (403 иначе).

    Роль перепроверяется по БД (H-4): ``is_admin`` из JWT-claim — лишь подсказка.
    Если у пользователя есть строка в ``users``, авторитетно её ``is_admin`` —
    тогда понижение роли через админку действует немедленно, не дожидаясь
    истечения куки. Для whitelist-операторов без строки в БД остаётся claim.
    """
    get_current_user = get_current_user_factory(jwt_secret, session_manager, allowed_user_ids)

    async def require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        is_admin = bool(user.get("is_admin"))
        if session_manager is not None:
            try:
                uid = int(user["user_id"])
            except (TypeError, ValueError, KeyError):
                uid = None
            if uid is not None:
                row = await asyncio.to_thread(session_manager.get_user_by_id, uid)
                if row is not None:
                    is_admin = bool(row.get("is_admin"))
        if not is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="admin only"
            )
        return user

    return require_admin


def can_manage_grant(*, actor_id: int, is_admin: bool, granted_by: int | None) -> bool:
    """Может ли actor управлять грантом (сменить/убрать).

    Админ — любым. Не-админ — только грантом, который выдал сам
    (``granted_by == actor_id``); админские/системные (``granted_by is None``)
    неприкосновенны.
    """
    if is_admin:
        return True
    return granted_by is not None and granted_by == actor_id


async def _resolve_manage_authority(
    session_manager, user: dict[str, Any], project_id: int, project_path: str,
) -> tuple[bool, bool]:
    """(is_admin, has_full_grant) — общая часть ``require_project_manage`` и
    ``can_manage_members``, вынесенная в одно место, чтобы оба места не могли
    разойтись и не дублировали запросы к БД.

    ``is_admin`` перепроверяется по БД (как ``require_admin``); ``has_full_grant``
    — явный full-грант ИМЕННО на этот проект (id-aware ``get_project_access_row``),
    НЕ whitelist-implicit ``resolve_project_access`` (F2 — тот отдаёт ``full``
    любому whitelist-оператору на ЛЮБОЙ проект, слишком широко для управления
    участниками).
    """
    is_admin = bool(user.get("is_admin"))
    try:
        uid = int(user["user_id"])
    except (TypeError, ValueError, KeyError):
        uid = None
    if uid is not None:
        row = await asyncio.to_thread(session_manager.get_user_by_id, uid)
        if row is not None:
            is_admin = bool(row.get("is_admin"))
    if is_admin or uid is None:
        return is_admin, False
    grant = await asyncio.to_thread(
        session_manager.get_project_access_row, uid, project_id, project_path
    )
    has_grant = grant is not None and grant["access_level"] == "full"
    return False, has_grant


async def can_manage_members(
    *, session_manager, user: dict[str, Any], project_id: int, project_path: str,
) -> bool:
    """Тот же предикат, что и ``require_project_manage`` (admin ИЛИ явный
    full-грант именно на этот проект) — переиспользуется, чтобы вычислить
    ``SessionOut.can_manage_members`` (L-8). Раньше фронт гейтил кнопку
    «Участники» клиентской эвристикой поверх ``resolve_project_access``
    (whitelist-implicit ``full``), из-за чего кнопка была видна и там, где
    серверный ``/members`` отвечал 403. Теперь оба места читают ОДИН и тот же
    источник правды — эта функция.
    """
    if session_manager is None:
        return False
    is_admin, has_grant = await _resolve_manage_authority(
        session_manager, user, project_id, project_path
    )
    return is_admin or has_grant


def require_project_manage_factory(
    jwt_secret: str, session_manager=None, allowed_user_ids=None,
    get_project_paths=None,
):
    """Dependency: пропускает, если юзер — админ ИЛИ имеет ЯВНЫЙ full-грант на
    ЭТОТ проект (по path-параметру ``project_id``). Возвращает контекст
    {user, project_id, project_path, is_admin}. 403 иначе, 404 если проекта нет,
    503 если storage недоступен.

    F2: право «управлять участниками» больше НЕ выводится из
    ``resolve_project_access`` (тот отдаёт ``full`` любому whitelist-оператору на
    ЛЮБОЙ проект — слишком широко). Управление требует явной строки-гранта
    ``access_level='full'`` именно на этом проекте (id-aware
    ``get_project_access_row``), либо роли админа (перепроверяется по БД).

    ``get_project_paths`` больше не используется этим гейтом (оставлен в
    сигнатуре ради обратной совместимости вызовов ``make_members_router`` /
    тестовой обвязки).
    """
    get_current_user = get_current_user_factory(jwt_secret, session_manager, allowed_user_ids)

    async def _check(project_id: int, user: dict[str, Any]) -> dict[str, Any]:
        if session_manager is None:
            raise HTTPException(status_code=503, detail="storage unavailable")
        abspath = await asyncio.to_thread(session_manager.get_project_abspath, project_id)
        if abspath is None:
            raise HTTPException(status_code=404, detail="Проект не найден")
        is_admin, has_grant = await _resolve_manage_authority(
            session_manager, user, project_id, abspath
        )
        if not (is_admin or has_grant):
            raise HTTPException(
                status_code=403, detail="Нужен полный доступ, чтобы управлять участниками"
            )
        return {
            "user": user, "project_id": project_id,
            "project_path": abspath, "is_admin": is_admin,
        }

    async def require_project_manage(
        project_id: int, user: dict[str, Any] = Depends(get_current_user)
    ) -> dict[str, Any]:
        return await _check(project_id, user)

    require_project_manage.__wrapped_check__ = _check
    return require_project_manage


async def require_owned_session(
    *,
    session_manager,
    session_uuid: str,
    user: dict[str, Any],
):
    """Look up a session and enforce that the current user owns it.

    Единая точка проверки владения сессией — раньше каждый роутер
    реализовывал её по-своему. Возвращает объект сессии или поднимает
    HTTP 404 (одинаково и для "не существует", и для "не твоя" — чтобы
    нельзя было перебором по UUID отличить одно от другого).
    """
    # Read-only ownership check — touch=False so a lookup doesn't turn into
    # a write+commit under the connection lock (CR3-7).
    session = await asyncio.to_thread(
        session_manager.get_session_by_uuid,
        session_uuid,
        owner_chat_id=int(user["user_id"]),
        touch=False,
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session not found",
        )
    return session
