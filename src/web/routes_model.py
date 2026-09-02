"""/api/model and /api/slash-commands routes.

Lets the web UI read & change Claude's model and discover the bot's
built-in slash commands. Mirrors the bot's /model handler in
src/bot/handlers/messages.py — reads/writes ~/.claude/settings.json.
"""
from __future__ import annotations

import asyncio

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.claude.claude_settings import (
    CLAUDE_SETTINGS_PATH,
    read_claude_settings,
    write_claude_settings,
)
from src.claude.commands import cli_command_label, fetch_cli_commands_for_project
from src.claude.model_catalog import catalog_model_ids, get_models
from src.claude.models import (
    ALIAS_TO_ID,
    DEFAULT_MODEL,
    is_safe_model_id,
)
from src.claude.skills import discover_marketplace_skills, discover_project_skills
from src.web.dependencies import get_current_user_factory, require_admin_factory
from src.web.project_access import resolve_project_access

logger = structlog.get_logger()

# Slash-commands exposed via /api/slash-commands.
# `target=bot` — обрабатывается ботом нативно (например, /model).
# `target=claude` — пробрасывается напрямую в Claude как user_message.
SLASH_COMMANDS: list[dict[str, str]] = [
    {"cmd": "/model", "label": "Сменить модель", "target": "bot", "kind": "command"},
    {"cmd": "/config", "label": "Показать ~/.claude/settings.json", "target": "bot", "kind": "command"},
    {"cmd": "/permissions", "label": "Режим разрешений", "target": "bot", "kind": "command"},
    {"cmd": "/mcp", "label": "MCP-серверы", "target": "bot", "kind": "command"},
    {"cmd": "/cost", "label": "Расход за сессию", "target": "claude", "kind": "command"},
    {"cmd": "/context", "label": "Использование контекстного окна", "target": "claude", "kind": "command"},
    {"cmd": "/compact", "label": "Сжать историю", "target": "claude", "kind": "command"},
    {"cmd": "/init", "label": "Сгенерировать CLAUDE.md", "target": "claude", "kind": "command"},
    {"cmd": "/review", "label": "Ревью текущего диффа", "target": "claude", "kind": "command"},
    {"cmd": "/debug", "label": "Диагностика проблемы", "target": "claude", "kind": "command"},
    {"cmd": "/simplify", "label": "Применить /code-review --fix", "target": "claude", "kind": "command"},
    {"cmd": "/verbose", "label": "Уровень детализации (0-3)", "target": "bot", "kind": "command"},
]


def build_slash_commands(
    *, project_path: str | None, claude_settings: dict
) -> list[dict[str, str]]:
    """Встроенные команды + скиллы плагинов + скиллы проекта (без CLI).
    Дедуп по cmd; встроенные приоритетнее. Используется в юнит-тестах и
    как синхронная база."""
    from pathlib import Path

    out: list[dict[str, str]] = list(SLASH_COMMANDS)
    seen = {c["cmd"] for c in out}
    skills: list[dict[str, str]] = list(discover_marketplace_skills(claude_settings))
    if project_path:
        skills.extend(discover_project_skills(Path(project_path)))
    for s in skills:
        if s["cmd"] in seen:
            continue
        seen.add(s["cmd"])
        out.append(s)
    return out


async def build_slash_commands_async(
    *, project_path: str | None, claude_settings: dict
) -> list[dict[str, str]]:
    """Полный список: встроенные + команды из CLI (как в боте) + скиллы.
    CLI-обнаружение кэшируется на час по проекту."""
    out: list[dict[str, str]] = list(SLASH_COMMANDS)
    seen = {c["cmd"] for c in out}

    cli_cmds = await fetch_cli_commands_for_project(project_path)
    for raw in cli_cmds:
        cmd = f"/{raw}"
        if cmd in seen:
            continue
        seen.add(cmd)
        out.append(
            {"cmd": cmd, "label": cli_command_label(raw), "target": "claude", "kind": "command"}
        )

    rest = build_slash_commands(
        project_path=project_path, claude_settings=claude_settings
    )
    for c in rest:
        if c["cmd"] in seen:
            continue
        seen.add(c["cmd"])
        out.append(c)
    return out


class ModelOut(BaseModel):
    current: str
    permission_mode: str
    known: list[dict[str, str]]


class ModelPatchIn(BaseModel):
    model: str


def make_model_router(
    *,
    jwt_secret: str,
    session_manager,
    project_paths_provider=None,
    allowed_user_ids: list[int] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["model"])
    whitelist = set(allowed_user_ids or [])
    get_current_user = get_current_user_factory(jwt_secret, session_manager, whitelist)
    require_admin = require_admin_factory(jwt_secret, session_manager, whitelist)

    @router.get("/model", response_model=ModelOut)
    async def get_model(_: dict = Depends(get_current_user)) -> ModelOut:
        data = await asyncio.to_thread(read_claude_settings, CLAUDE_SETTINGS_PATH)
        raw_model = str(data.get("model") or DEFAULT_MODEL)
        # Каталог ходит в сеть (с кэшем на часы) — уводим в тред, чтобы не
        # блокировать событийный цикл; при недоступности отдаёт статику.
        known = await asyncio.to_thread(get_models)
        return ModelOut(
            current=ALIAS_TO_ID.get(raw_model, raw_model),
            permission_mode=str(
                data.get("permissions", {}).get("defaultMode", "default")
            ),
            known=known,
        )

    @router.patch("/model", response_model=ModelOut)
    async def patch_model(
        payload: ModelPatchIn,
        user: dict = Depends(require_admin),
    ) -> ModelOut:
        # Только админ: модель пишется в ОБЩИЙ ~/.claude/settings.json, который
        # читает каждый спавн Claude (всех веб-юзеров и бота). Раньше менять её
        # мог любой залогиненный, включая readonly-аккаунт.
        new_model = payload.model.strip()
        # Allowlist — значение пишется дословно в общий ~/.claude/settings.json,
        # который читает каждый спавн Claude (и бот), поэтому произвольная или
        # разбухшая строка туда попасть не должна (CR3-3). Список теперь живой
        # (модели выходят часто), но формат id всё равно проверяем строго: без
        # этого недоступный/подменённый справочник расширил бы allowlist.
        valid_ids = await asyncio.to_thread(catalog_model_ids)
        if not is_safe_model_id(new_model) or new_model not in valid_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown model; expected one of {sorted(valid_ids)}",
            )
        data = await asyncio.to_thread(read_claude_settings, CLAUDE_SETTINGS_PATH)
        data["model"] = new_model
        await asyncio.to_thread(write_claude_settings, data, CLAUDE_SETTINGS_PATH)
        logger.info("claude_model_changed", model=new_model, user_id=user["user_id"])

        # Clear session_id on all of the caller's sessions so the next
        # message starts fresh with the new model (mirrors the bot's
        # /model handler).
        if session_manager is not None:
            def _clear_user_sessions() -> None:
                sessions = session_manager.get_all_sessions(
                    owner_chat_id=int(user["user_id"])
                )
                for s in sessions:
                    if s.session_id:
                        session_manager.clear_session_id(s.topic_id)

            try:
                await asyncio.to_thread(_clear_user_sessions)
            except Exception as exc:
                # Non-fatal — model is already saved. Worst case: the
                # user's next message resumes the old session under the
                # new model (Claude usually accepts the swap).
                logger.warning(
                    "model_change_session_clear_failed", error=str(exc)
                )

        return ModelOut(
            current=new_model,
            permission_mode=str(
                data.get("permissions", {}).get("defaultMode", "default")
            ),
            known=await asyncio.to_thread(get_models),
        )

    @router.get("/slash-commands")
    async def get_slash_commands(
        project_path: str | None = None,
        user: dict = Depends(get_current_user),
    ) -> list[dict[str, str]]:
        # Авторизация project_path ДО скана/спавна: иначе любой юзер мог
        # запустить `claude --dangerously-skip-permissions` с произвольным
        # cwd и прочитать <project_path>/.claude/skills/*. Без project_path —
        # только встроенные команды (скан не выполняется).
        if project_path and project_paths_provider is not None:
            access = resolve_project_access(
                project_path,
                user=user,
                project_paths=project_paths_provider(),
                whitelist=whitelist,
                session_manager=session_manager,
            )
            if not access.allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="project not allowed",
                )
        data = await asyncio.to_thread(read_claude_settings, CLAUDE_SETTINGS_PATH)
        return await build_slash_commands_async(
            project_path=project_path, claude_settings=data
        )

    return router
