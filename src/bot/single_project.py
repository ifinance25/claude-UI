"""Привязка топика к проекту без лишнего вопроса, если проект один.

Если в настройках только один каталог, хендлеры сразу привязывают топик.
Если проектов несколько, человек выбирает через /projects и клавиатуру.
"""
from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger()


def single_project(settings) -> Path | None:
    """Единственный проект light или None, если каталог проектов пуст."""
    paths = settings.get_light_project_paths()
    return paths[0] if paths else None


async def bind_topic_to_single_project(
    *,
    settings,
    session_manager,
    topic_id: int,
    chat_id: int,
) -> Path | None:
    """Создаёт сессию топика в единственном проекте.

    Возвращает путь проекта, если привязка состоялась, и None, если проектов
    нет — тогда вызывающий показывает подсказку про PROJECTS_DIR.
    """
    project = single_project(settings)
    if project is None:
        return None

    await session_manager.async_create_session(
        topic_id=topic_id,
        project_path=str(project),
        project_name=project.name,
        chat_id=chat_id,
    )
    logger.info("project_bound_automatically", topic_id=topic_id, project=project.name)
    return project
