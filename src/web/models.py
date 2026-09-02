"""Pydantic schemas for the web REST API.

Kept separate from src.event_bus.events so REST contracts can evolve
independently of internal bus shapes.
"""
from __future__ import annotations

from pydantic import BaseModel


class ProjectOut(BaseModel):
    name: str
    path: str


class SessionOut(BaseModel):
    topic_id: int
    session_uuid: str
    project_path: str
    project_name: str
    # Числовой id проекта (projects.id); None для сессий «без проекта».
    # Фронту нужен как ключ /api/projects/{project_id}/members.
    project_id: int | None = None
    session_id: str | None
    status: str
    message_count: int
    total_cost_usd: float
    created_at: str
    last_activity: str
    notes: str | None = None
    # True, если по сессии прямо сейчас идёт генерация Claude (in-flight).
    # Заполняется листингом из RunningSessionsTracker; фронт рисует
    # индикатор «думает». Default False — для каналов без трекера.
    is_running: bool = False
    # L-8: может ли текущий пользователь управлять участниками проекта этой
    # сессии — вычислено ТЕМ ЖЕ предикатом, что и серверный require_project_manage
    # (src.web.dependencies.can_manage_members), а не клиентской эвристикой поверх
    # resolve_project_access (та шире и даёт рассинхрон с /members → 403).
    # False для сессий без проекта (project_id is None).
    can_manage_members: bool = False


class SessionCreateIn(BaseModel):
    # Оба поля опциональны: None/"" → сессия «без проекта» (чистый Claude).
    # Канон хранения — пустая строка (см. routes_sessions.create_session_route
    # и COALESCE в session.py), поэтому SessionOut.project_path остаётся str.
    project_path: str | None = None
    project_name: str | None = None


class SessionPatchIn(BaseModel):
    notes: str | None = None


class MessageOut(BaseModel):
    event_id: int
    topic_id: int
    request_id: str | None
    type: str
    kind: str | None
    content: str
    metadata: dict | None
    created_at: str
