"""Привязка топика к единственному проекту — без вопроса человеку.

Light работает с одним проектом: выбор проекта вырезан из веб-интерфейса, там
чат сразу создаётся в единственном каталоге. В Telegram оставался выбор —
клавиатура с одной кнопкой и команда /projects, чтобы позвать её заново. Это
расходилось с вебом и требовало лишнего шага там, где выбирать не из чего.

Модуль держит одну заботу: получить единственный проект и привязать к нему
топик. Хендлеры зовут его вместо показа клавиатуры.
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
