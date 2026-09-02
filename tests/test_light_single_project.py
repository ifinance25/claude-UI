"""Light показывает один проект — обоими входами, а не только web-only.

Ограничение «light работает с одним проектом» жило в scripts/run_web.py, то есть
применялось, только когда Telegram НЕ настроен. При настроенном боте сервис
поднимается через `python -m src.main`, и тот отдавал веб-серверу полный список.

Проверено на живом сервере: в Telegram-режиме GET /api/projects возвращал два
проекта вместо одного. Свойство, вокруг которого построена вся light-версия,
исчезало от штатного действия — подключения бота, — молча и без сообщений.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import Settings

SRC = Path(__file__).resolve().parent.parent


@pytest.fixture
def settings_with_projects(tmp_path: Path, monkeypatch) -> Settings:
    projects = tmp_path / "projects"
    for name in ("alpha", "beta", "gamma"):
        (projects / name).mkdir(parents=True)
    # PROJECTS_DIR из окружения имеет приоритет над конфигом — в тесте он
    # подхватил бы .env разработчика и проверял чужой каталог.
    monkeypatch.setenv("PROJECTS_DIR", str(projects))
    return Settings(projects={"scan_directory": str(projects)})


def test_light_returns_single_project(settings_with_projects):
    assert len(settings_with_projects.get_project_paths()) == 3
    light = settings_with_projects.get_light_project_paths()
    assert len(light) == 1, f"light обязан показывать один проект, получено {light}"
    assert light[0].name == "alpha", "берётся первый по алфавиту"


def test_light_survives_empty_projects_dir(tmp_path: Path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PROJECTS_DIR", str(empty))
    settings = Settings(projects={"scan_directory": str(empty)})
    assert settings.get_light_project_paths() == []


@pytest.mark.parametrize(
    "entrypoint",
    [SRC / "src" / "bot" / "core.py", SRC / "scripts" / "run_web.py"],
    ids=["telegram+web", "web-only"],
)
def test_both_entrypoints_use_the_light_list(entrypoint: Path):
    """Оба входа обязаны звать get_light_project_paths.

    Расхождение вернётся ровно в тот момент, когда один из входов снова возьмёт
    полный список, — а заметно это станет только на живом сервере с двумя
    проектами в PROJECTS_DIR. Поэтому проверка стоит здесь.
    """
    text = entrypoint.read_text(encoding="utf-8")
    assert "get_light_project_paths()" in text, f"{entrypoint.name} не зовёт общий список"
    passes_full_list = "project_paths=settings.get_project_paths()" in text
    assert not passes_full_list, f"{entrypoint.name} передаёт полный список проектов"
