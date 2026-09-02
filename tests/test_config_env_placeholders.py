"""Нераскрытый ${VAR} в config.yaml — отсутствующее значение, а не текст.

`os.path.expandvars` оставляет `${VAR}` буквально, если переменной нет. Для
`telegram.token: "${TELEGRAM_BOT_TOKEN}"` это ломало всю защиту старта: проверка
`if not settings.get_bot_token()` видела непустую строку-плейсхолдер, пропускала
запуск, и приложение падало уже внутри aiogram с «Token is invalid!».

На живом сервере это выглядело так: человек убрал TELEGRAM_BOT_TOKEN из .env,
перезапустил сервис — юнит ушёл в рестарт-петлю (NRestarts рос непрерывно), а
вместе с ботом лёг и веб-интерфейс, потому что `-m src.main` поднимает оба в
одном процессе. Снаружи это 502 без единого намёка на причину.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import Settings, _expand_env

CONFIG = """
telegram:
  token: "${TELEGRAM_BOT_TOKEN}"
  chat_id: null
projects:
  scan_directory: "${PROJECTS_DIR}"
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_missing_env_yields_empty_not_placeholder(config_file, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    settings = Settings.from_yaml(config_file)
    token = settings.get_bot_token()
    assert token == "", f"вместо пустого значения пришёл {token!r}"
    # Именно это условие проверяет src/main.py перед стартом бота.
    assert not token, "валидация «нет токена» обязана срабатывать"


def test_present_env_is_substituted(config_file, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAFabc")
    settings = Settings.from_yaml(config_file)
    assert settings.get_bot_token() == "123456:AAFabc"


def test_placeholder_stripped_only_when_unresolved(monkeypatch):
    monkeypatch.delenv("NOPE_UNSET_VAR", raising=False)
    monkeypatch.setenv("SET_VAR", "value")
    assert _expand_env("a=${NOPE_UNSET_VAR}") == "a="
    assert _expand_env("a=${SET_VAR}") == "a=value"
    # Текст, похожий на плейсхолдер, но не являющийся им, не трогаем.
    assert _expand_env("cost: $10 {braces}") == "cost: $10 {braces}"
