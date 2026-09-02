"""/api/settings — minimal user-level preferences endpoint.

Currently exposes only ``verbose_level`` (0-3), mirroring the bot's
/verbose command. The value is stored on the user record so GET and
PATCH are symmetric and per-session overrides set via the bot's
``/verbose`` are preserved.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from src.web.dependencies import get_current_user_factory

VERBOSE_NAMES: dict[int, str] = {
    0: "Тихий",
    1: "Нормальный",
    2: "Подробный",
    3: "Детальный",
}


class SettingsOut(BaseModel):
    verbose_level: Literal[0, 1, 2, 3]


class SettingsPatchIn(BaseModel):
    verbose_level: int = Field(..., ge=0, le=3)


def make_settings_router(
    *, jwt_secret: str, session_manager, allowed_user_ids: list[int] | None = None
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["settings"])
    get_current_user = get_current_user_factory(
        jwt_secret, session_manager, set(allowed_user_ids or [])
    )

    @router.get("/settings", response_model=SettingsOut)
    async def get_settings(user: dict = Depends(get_current_user)) -> SettingsOut:
        if session_manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="session manager unavailable",
            )
        level = session_manager.get_user_default_verbose(int(user["user_id"]))
        return SettingsOut(verbose_level=level)  # type: ignore[arg-type]

    @router.patch("/settings", response_model=SettingsOut)
    async def patch_settings(
        payload: SettingsPatchIn,
        user: dict = Depends(get_current_user),
    ) -> SettingsOut:
        if session_manager is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="session manager unavailable",
            )
        session_manager.set_user_verbose(
            int(user["user_id"]), payload.verbose_level
        )
        return SettingsOut(verbose_level=payload.verbose_level)  # type: ignore[arg-type]

    return router
