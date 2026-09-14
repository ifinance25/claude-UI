"""Админ-панель: CRUD пользователей и выдача доступов к проектам.
Все роуты за require_admin."""
from __future__ import annotations

import asyncio
import sqlite3

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.web.dependencies import require_admin_factory
from src.web.passwords import hash_password

# M-3: каталоги, которые НЕЛЬЗЯ делать корнем проекта — иначе readonly-грант на
# такой «проект» открыл бы чтение всей ФС (/etc/shadow, чужие .env, ключи).
# Запрещаем сам каталог и всё под ним.
_FORBIDDEN_PROJECT_TREES = (
    "/etc", "/root", "/sys", "/proc", "/dev", "/boot", "/run",
)


def _resolve_project_abspath(raw: str, projects_root: Path) -> str:
    """Имя проекта (CRM) или путь внутри PROJECTS_DIR → абсолютный путь.

    Пользователь не задаёт системные пути. Корень каталога берётся из настроек
    (PROJECTS_DIR). Ввод вроде /CRM не создаёт каталог в корне диска.
    """
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="укажите имя проекта")
    root = projects_root.expanduser().resolve()
    candidate = Path(text).expanduser()
    inside_root = False
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
            inside_root = True
            dest = resolved
        except ValueError:
            dest = None
        if not inside_root:
            # /CRM или /etc/... : только односегментное имя поднимаем в PROJECTS_DIR
            name = text.strip("/")
            if (not name) or "/" in name or "\\" in name or name in (".", ".."):
                raise HTTPException(
                    status_code=400,
                    detail="укажите имя проекта, не путь на диске",
                )
            dest = (root / name).resolve()
    else:
        name = text
        if "/" in name or "\\" in name or name in (".", ".."):
            raise HTTPException(
                status_code=400,
                detail="укажите имя проекта, не путь на диске",
            )
        dest = (root / name).resolve()
    try:
        dest.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=400, detail="проект должен лежать внутри каталога проектов"
        )
    posix = dest.as_posix()
    if posix in ("/", "") or dest == dest.parent:
        raise HTTPException(
            status_code=400, detail="abspath must not be a filesystem root"
        )
    for tree in _FORBIDDEN_PROJECT_TREES:
        if posix == tree or posix.startswith(tree + "/"):
            raise HTTPException(
                status_code=400, detail="abspath points at a system directory"
            )
    return str(dest)

# Единый канон уровней доступа во всём стеке. Резолвер (project_access.py)
# понимает только бинарность readonly/full: любое иное значение трактуется
# как full — поэтому приём "Owner"/"Admin"/"Member"/"read-only" был
# fail-open (тихая выдача полного доступа). Жёстко ограничиваем набор.
AccessLevel = Literal["full", "readonly"]


class _UserCreate(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class _PasswordReset(BaseModel):
    password: str


class _AdminFlag(BaseModel):
    is_admin: bool


class _AccessIn(BaseModel):
    project_path: str
    access_level: AccessLevel = "full"


class ProjectCreate(BaseModel):
    """Запрос на создание/переименование проекта — несёт путь проекта."""

    abspath: str


class ProjectResponse(BaseModel):
    """Ответ с записью проекта (id + путь)."""

    id: int
    abspath: str


class AccessCreate(BaseModel):
    """Запрос на выдачу доступа к проекту."""

    user_id: int
    project_id: int
    access_level: AccessLevel = "full"


class AccessUpdate(BaseModel):
    """Запрос на изменение уровня доступа (PATCH) — только уровень."""

    access_level: AccessLevel


class AccessResponse(BaseModel):
    """Ответ с записью о доступе."""

    id: int
    user_id: int
    # username nullable: telegram-юзеры без @username и verbose-upsert
    # пишут NULL — иначе Pydantic роняет весь GET /accesses в 500.
    username: str | None
    project_id: int | None
    project_path: str | None
    access_level: str


def make_admin_router(
    *,
    jwt_secret: str,
    session_manager,
    allowed_user_ids: list[int] | None = None,
    api_key_store=None,
    projects_dir: Path | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])
    require_admin = require_admin_factory(
        jwt_secret, session_manager, set(allowed_user_ids or [])
    )
    projects_root = Path(projects_dir or "/var/lib/vels-bot/projects")

    @router.get("/users")
    async def list_users(_: dict = Depends(require_admin)) -> list[dict]:
        # Обогащаем каждую строку НЕсекретным индикатором per-user ключа (SP2):
        # has_key/key_status/key_last4. Сам ключ НИКОГДА не покидает стор —
        # get_meta не расшифровывает и не возвращает его. Если api_key_store
        # is None (CONNECTIONS_SECRET_KEY не задан) — фича выключена, has_key=false.
        def _build() -> list[dict]:
            users = session_manager.list_users()
            for u in users:
                meta = (
                    api_key_store.get_meta(u["user_id"])
                    if api_key_store is not None
                    else None
                )
                u["has_key"] = meta is not None
                u["key_status"] = meta["status"] if meta else None
                u["key_last4"] = meta["last4"] if meta else None
            return users

        return await asyncio.to_thread(_build)

    @router.post("/users")
    async def create_user(
        payload: _UserCreate, _: dict = Depends(require_admin)
    ) -> dict:
        if len(payload.password) < 12:
            raise HTTPException(
                status_code=422, detail="password too short (min 12 chars)"
            )
        if not payload.username.strip():
            raise HTTPException(status_code=422, detail="username required")
        existing = await asyncio.to_thread(
            session_manager.get_user_by_username, payload.username
        )
        if existing:
            raise HTTPException(status_code=409, detail="username exists")
        try:
            uid = await asyncio.to_thread(
                session_manager.create_local_user,
                username=payload.username,
                password_hash=hash_password(payload.password),
                is_admin=payload.is_admin,
            )
        except ValueError:
            # Гонка проверки-вставки (L-14): UNIQUE-индекс поймал дубль логина.
            raise HTTPException(status_code=409, detail="username exists")
        return {
            "user_id": uid,
            "username": payload.username,
            "is_admin": payload.is_admin,
        }

    @router.delete("/users/{user_id}")
    async def delete_user(
        user_id: int, admin: dict = Depends(require_admin)
    ) -> dict:
        # Нельзя удалить самого себя — защита от случайного self-lockout.
        if int(admin.get("user_id", 0)) == user_id:
            raise HTTPException(status_code=400, detail="cannot delete yourself")
        ok = await asyncio.to_thread(session_manager.delete_user, user_id)
        if not ok:
            raise HTTPException(status_code=404, detail="user not found")
        return {"ok": True}

    @router.post("/users/{user_id}/password")
    async def reset_password(
        user_id: int, payload: _PasswordReset, _: dict = Depends(require_admin)
    ) -> dict:
        if len(payload.password) < 12:
            raise HTTPException(
                status_code=422, detail="password too short (min 12 chars)"
            )
        ok = await asyncio.to_thread(
            session_manager.set_user_password, user_id, hash_password(payload.password)
        )
        if not ok:
            raise HTTPException(status_code=404, detail="user not found")
        return {"ok": True}

    @router.post("/users/{user_id}/active")
    async def set_active(
        user_id: int, active: bool, admin: dict = Depends(require_admin)
    ) -> dict:
        # Нельзя отключить самого себя — иначе единственный админ
        # запирает себе вход (bootstrap не реактивирует существующего).
        if int(admin.get("user_id", 0)) == user_id and not active:
            raise HTTPException(
                status_code=400, detail="cannot deactivate yourself"
            )
        ok = await asyncio.to_thread(session_manager.set_user_active, user_id, active)
        if not ok:
            raise HTTPException(status_code=404, detail="user not found")
        return {"ok": True}

    @router.post("/users/{user_id}/admin")
    async def set_admin(
        user_id: int, payload: _AdminFlag, admin: dict = Depends(require_admin)
    ) -> dict:
        # Нельзя разжаловать самого себя — иначе единственный админ теряет
        # доступ к админке (self-lockout), как у self-delete/self-deactivate.
        # Само-повышение (is_admin=true на своём id) безвредно и разрешено.
        if int(admin.get("user_id", 0)) == user_id and not payload.is_admin:
            raise HTTPException(status_code=400, detail="cannot demote yourself")
        ok = await asyncio.to_thread(
            session_manager.set_user_admin, user_id, payload.is_admin
        )
        if not ok:
            raise HTTPException(status_code=404, detail="user not found")
        return {"ok": True}

    @router.get("/users/{user_id}/access")
    async def list_access(
        user_id: int, _: dict = Depends(require_admin)
    ) -> list[dict]:
        return await asyncio.to_thread(session_manager.list_project_access, user_id)

    @router.post("/users/{user_id}/access")
    async def grant_access_by_path(
        user_id: int, payload: _AccessIn, _: dict = Depends(require_admin)
    ) -> dict:
        # access_level валидируется Literal в _AccessIn (422 при ином значении).
        await asyncio.to_thread(
            session_manager.set_project_access,
            user_id,
            payload.project_path,
            payload.access_level,
        )
        return {"ok": True}

    @router.delete("/users/{user_id}/access")
    async def revoke_access_by_path(
        user_id: int, project_path: str, _: dict = Depends(require_admin)
    ) -> dict:
        await asyncio.to_thread(
            session_manager.revoke_project_access, user_id, project_path
        )
        return {"ok": True}

    # ── CRUD проектов ───────────────────────────────────────────────

    @router.get("/projects", response_model=list[ProjectResponse])
    async def list_projects(
        _: dict = Depends(require_admin),
    ) -> list[ProjectResponse]:
        rows = await asyncio.to_thread(session_manager.list_all_projects)
        return [ProjectResponse(id=r["id"], abspath=r["abspath"]) for r in rows]

    @router.post("/projects", response_model=ProjectResponse)
    async def create_project(
        payload: ProjectCreate, _: dict = Depends(require_admin)
    ) -> ProjectResponse:
        abspath = _resolve_project_abspath(payload.abspath, projects_root)
        # Каталог может ещё не существовать на диске — создаём его сами, чтобы
        # проект можно было завести целиком из веб-панели, без захода по SSH.
        # Путь уже прошёл проверку M-3 (не системное дерево, не корень ФС) и
        # запрос авторизован require_admin — mkdir здесь безопасен.
        try:
            await asyncio.to_thread(
                Path(abspath).mkdir, parents=True, exist_ok=True
            )
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"cannot create directory: {exc}"
            )
        try:
            row = await asyncio.to_thread(
                session_manager.create_project, abspath
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="abspath required")
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="project already exists")
        return ProjectResponse(id=row["id"], abspath=row["abspath"])

    @router.patch("/projects/{project_id}", response_model=ProjectResponse)
    async def rename_project(
        project_id: int, payload: ProjectCreate, _: dict = Depends(require_admin)
    ) -> ProjectResponse:
        abspath = _resolve_project_abspath(payload.abspath, projects_root)
        try:
            row = await asyncio.to_thread(
                session_manager.rename_project, project_id, abspath
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="abspath required")
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="project already exists")
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return ProjectResponse(id=row["id"], abspath=row["abspath"])

    @router.delete("/projects/{project_id}")
    async def delete_project(
        project_id: int, _: dict = Depends(require_admin)
    ) -> dict:
        ok = await asyncio.to_thread(session_manager.delete_project, project_id)
        if not ok:
            raise HTTPException(status_code=404, detail="project not found")
        return {"success": True}

    # ── CRUD доступов (проект-пользователь) ─────────────────────────────

    @router.get("/accesses", response_model=list[AccessResponse])
    async def list_accesses(
        _: dict = Depends(require_admin),
    ) -> list[AccessResponse]:
        rows = await asyncio.to_thread(session_manager.list_all_accesses)
        return [
            AccessResponse(
                id=r["id"],
                user_id=r["user_id"],
                username=r["username"],
                project_id=r["project_id"],
                project_path=r["project_path"],
                access_level=r["access_level"],
            )
            for r in rows
        ]

    @router.post("/accesses", response_model=AccessResponse)
    async def create_access(
        payload: AccessCreate, admin: dict = Depends(require_admin)
    ) -> AccessResponse:
        # Safety: нельзя выдавать/менять собственный доступ — иначе POST-upsert
        # обходил бы self-guard на PATCH/DELETE (см. аудит). Админ и так имеет
        # полный доступ ко всем проектам.
        if int(admin.get("user_id", 0)) == payload.user_id:
            raise HTTPException(status_code=400, detail="cannot modify your own access")
        # 409 на дубликат (спека): не молчаливый upsert.
        existing = await asyncio.to_thread(
            session_manager.find_access, payload.user_id, payload.project_id
        )
        if existing:
            raise HTTPException(status_code=409, detail="access already exists")
        try:
            row = await asyncio.to_thread(
                session_manager.grant_project_access_by_id,
                payload.user_id,
                payload.project_id,
                payload.access_level,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Fetch full record with username and project path
        full = await asyncio.to_thread(
            session_manager.get_access_by_id, row["id"]
        )
        if not full:
            raise HTTPException(status_code=500, detail="failed to fetch created access")
        return AccessResponse(
            id=full["id"],
            user_id=full["user_id"],
            username=full["username"],
            project_id=full["project_id"],
            project_path=full["project_path"],
            access_level=full["access_level"],
        )

    @router.patch("/accesses/{access_id}", response_model=AccessResponse)
    async def update_access(
        access_id: int,
        payload: AccessUpdate,
        admin: dict = Depends(require_admin),
    ) -> AccessResponse:
        # Safety: cannot change own access
        current = await asyncio.to_thread(
            session_manager.get_access_by_id, access_id
        )
        if not current:
            raise HTTPException(status_code=404, detail="access not found")
        if int(admin.get("user_id", 0)) == current["user_id"]:
            raise HTTPException(status_code=400, detail="cannot modify your own access")
        ok = await asyncio.to_thread(
            session_manager.update_access_level, access_id, payload.access_level
        )
        if not ok:
            raise HTTPException(status_code=404, detail="access not found")
        # Fetch updated record
        full = await asyncio.to_thread(
            session_manager.get_access_by_id, access_id
        )
        if not full:
            raise HTTPException(status_code=500, detail="failed to fetch updated access")
        return AccessResponse(
            id=full["id"],
            user_id=full["user_id"],
            username=full["username"],
            project_id=full["project_id"],
            project_path=full["project_path"],
            access_level=full["access_level"],
        )

    @router.delete("/accesses/{access_id}")
    async def revoke_access_by_id(
        access_id: int, admin: dict = Depends(require_admin)
    ) -> dict:
        # Safety: cannot revoke own access
        current = await asyncio.to_thread(
            session_manager.get_access_by_id, access_id
        )
        if not current:
            raise HTTPException(status_code=404, detail="access not found")
        if int(admin.get("user_id", 0)) == current["user_id"]:
            raise HTTPException(status_code=400, detail="cannot revoke your own access")
        ok = await asyncio.to_thread(
            session_manager.revoke_access_by_id, access_id
        )
        if not ok:
            raise HTTPException(status_code=404, detail="access not found")
        return {"success": True}

    return router
