"""Self-service управление участниками проекта (не админка).

Право = full-доступ к проекту (require_project_manage). Не-админ управляет только
СВОИМИ грантами (granted_by == он); админские (granted_by IS NULL) неприкосновенны.
"""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.web.dependencies import can_manage_grant, require_project_manage_factory

AccessLevel = Literal["full", "readonly"]


def _resolve_identifier(session_manager, identifier: str) -> int | None:
    """id/username существующего юзера → user_id, иначе None."""
    ident = identifier.strip().lstrip("@")
    if not ident:
        return None
    if ident.isdigit():
        row = session_manager.get_user_by_id(int(ident))
        return int(ident) if row is not None else None
    row = session_manager.get_user_by_username(ident)
    return int(row["user_id"]) if row else None


class _MemberOut(BaseModel):
    user_id: int
    username: str | None = None
    access_level: str
    manageable: bool


class _AddMemberIn(BaseModel):
    identifier: str
    access_level: AccessLevel = "readonly"


class _PatchMemberIn(BaseModel):
    access_level: AccessLevel


def make_members_router(
    *, jwt_secret: str, session_manager=None, allowed_user_ids=None,
    get_project_paths=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/projects", tags=["members"])
    require_manage = require_project_manage_factory(
        jwt_secret=jwt_secret, session_manager=session_manager,
        allowed_user_ids=allowed_user_ids, get_project_paths=get_project_paths,
    )

    @router.get("/{project_id}/members", response_model=list[_MemberOut])
    async def list_members(ctx: dict = Depends(require_manage)) -> list[dict]:
        actor_id = int(ctx["user"]["user_id"])
        rows = await asyncio.to_thread(
            session_manager.list_project_members, ctx["project_id"], ctx["project_path"]
        )
        return [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "access_level": r["access_level"],
                "manageable": can_manage_grant(
                    actor_id=actor_id, is_admin=ctx["is_admin"],
                    granted_by=r["granted_by"],
                ) and r["user_id"] != actor_id,
            }
            for r in rows
        ]

    @router.post("/{project_id}/members")
    async def add_member(payload: _AddMemberIn, ctx: dict = Depends(require_manage)):
        target = await asyncio.to_thread(
            _resolve_identifier, session_manager, payload.identifier
        )
        if target is None:
            # F4 (осознанно принято): 404 на несуществующего = слабый
            # user-enumeration oracle (актор с full-грантом может перебором
            # узнать, какие id/username существуют). Оставляем: чёткая ошибка
            # важнее ничтожной утечки факта существования, а актор уже доверенный
            # (имеет full на проекте). Секреты не раскрываются.
            raise HTTPException(
                status_code=404,
                detail="Пользователь не найден — пусть сначала войдёт в систему.",
            )
        actor_id = int(ctx["user"]["user_id"])
        # Уже существующий грант перезаписывать можно только если актор вправе
        # им управлять (тот же критерий, что у PATCH/DELETE). Иначе POST через
        # ON CONFLICT ...DO UPDATE SET granted_by=excluded.granted_by позволил бы
        # не-админу «присвоить» админский/чужой грант и молча переопределить его.
        existing = await asyncio.to_thread(
            session_manager.get_project_access_row,
            target, ctx["project_id"], ctx["project_path"],
        )
        if existing is not None and not can_manage_grant(
            actor_id=actor_id, is_admin=ctx["is_admin"],
            granted_by=existing["granted_by"],
        ):
            raise HTTPException(status_code=403, detail="Этот доступ выдал не вы.")
        await asyncio.to_thread(
            session_manager.set_project_access,
            target, ctx["project_path"], payload.access_level, actor_id,
        )
        return {"user_id": target, "access_level": payload.access_level}

    async def _assert_manageable(ctx: dict, target_user_id: int) -> int:
        actor_id = int(ctx["user"]["user_id"])
        if target_user_id == actor_id:
            raise HTTPException(status_code=400, detail="Нельзя менять свой доступ.")
        row = await asyncio.to_thread(
            session_manager.get_project_access_row,
            target_user_id, ctx["project_id"], ctx["project_path"],
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Участник не найден")
        if not can_manage_grant(actor_id=actor_id, is_admin=ctx["is_admin"],
                                granted_by=row["granted_by"]):
            raise HTTPException(status_code=403, detail="Этот доступ выдал не вы.")
        return actor_id

    @router.patch("/{project_id}/members/{target_user_id}")
    async def patch_member(target_user_id: int, payload: _PatchMemberIn,
                           ctx: dict = Depends(require_manage)):
        actor_id = await _assert_manageable(ctx, target_user_id)
        await asyncio.to_thread(
            session_manager.set_project_access,
            target_user_id, ctx["project_path"], payload.access_level, actor_id,
        )
        return {"user_id": target_user_id, "access_level": payload.access_level}

    @router.delete("/{project_id}/members/{target_user_id}")
    async def delete_member(target_user_id: int, ctx: dict = Depends(require_manage)):
        await _assert_manageable(ctx, target_user_id)
        await asyncio.to_thread(
            session_manager.revoke_project_access_for_project,
            target_user_id, ctx["project_id"], ctx["project_path"],
        )
        return {"removed": target_user_id}

    return router
