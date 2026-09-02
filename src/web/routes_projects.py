"""/api/projects routes."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Depends

from src.web.dependencies import get_current_user_factory
from src.web.models import ProjectOut
from src.web.project_access import resolve_project_access


def make_projects_router(
    *,
    jwt_secret: str,
    project_paths_provider: Callable[[], list],
    session_manager=None,
    allowed_user_ids: list[int] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["projects"])
    whitelist = set(allowed_user_ids or [])
    get_current_user = get_current_user_factory(jwt_secret, session_manager, whitelist)

    @router.get("/projects", response_model=list[ProjectOut])
    async def list_projects(
        user: dict = Depends(get_current_user),
    ) -> list[ProjectOut]:
        # Через единый резолвер (как docs/sessions/ws/model/uploads): проект
        # виден, если resolve_project_access().allowed. Режим без БД / админ /
        # Telegram-операторы (whitelist) — видят все настроенные; локальные —
        # только выданные. Нормализация пути — там же.
        pp = project_paths_provider()
        all_paths = [str(p) for p in pp]

        def _visible() -> list[str]:
            return [
                p
                for p in all_paths
                if resolve_project_access(
                    p,
                    user=user,
                    project_paths=pp,
                    whitelist=whitelist,
                    session_manager=session_manager,
                ).allowed
            ]

        paths = await asyncio.to_thread(_visible)
        return [ProjectOut(name=Path(p).name, path=p) for p in paths]

    return router
