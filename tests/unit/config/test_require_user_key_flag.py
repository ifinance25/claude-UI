"""Тесты флага ``require_user_key`` (SP2, Task 14).

Флаг задаёт fail-closed политику: не-привилегированные пользователи должны
подставлять собственный Anthropic-ключ. По умолчанию True. Источник значения:
env ``CLAUDE_REQUIRE_USER_KEY`` (регистронезависимо) > ``claude.require_user_key``
из конфига. Читается как ``getattr(settings, "require_user_key", True)``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import ClaudeSettings, Settings

_ENV_VAR = "CLAUDE_REQUIRE_USER_KEY"


def _write_yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_default_value_is_true(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fail-closed: без явной настройки флаг == True."""
    monkeypatch.delenv(_ENV_VAR, raising=False)

    # ClaudeSettings-дефолт
    assert ClaudeSettings().require_user_key is True

    # Конфиг без ключа require_user_key → дефолт сохраняется.
    cfg = _write_yaml(tmp_path, "claude:\n  transport: sdk\n")
    settings = Settings.from_yaml(cfg)
    assert settings.require_user_key is True
    assert settings.claude.require_user_key is True


def test_field_exists_on_settings_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Атрибут доступен на верхнем уровне Settings и является bool."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    settings = Settings.from_yaml(_write_yaml(tmp_path, "claude:\n  transport: sdk\n"))

    assert hasattr(settings, "require_user_key")
    assert isinstance(settings.require_user_key, bool)
    # Read-паттерн из relay/бота (Tasks 7-8).
    assert getattr(settings, "require_user_key", True) is True


def test_config_file_value_is_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Значение из claude-секции конфига корректно читается."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    cfg = _write_yaml(tmp_path, "claude:\n  require_user_key: false\n")
    settings = Settings.from_yaml(cfg)

    assert settings.claude.require_user_key is False
    assert settings.require_user_key is False


def test_env_var_overrides_to_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``CLAUDE_REQUIRE_USER_KEY=false`` переопределяет конфиг (true → false)."""
    monkeypatch.setenv(_ENV_VAR, "false")
    cfg = _write_yaml(tmp_path, "claude:\n  require_user_key: true\n")
    settings = Settings.from_yaml(cfg)

    assert settings.require_user_key is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
    ],
)
def test_env_var_parsing_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str, expected: bool
) -> None:
    """Разбор env-флага регистронезависим и принимает распространённые формы."""
    monkeypatch.setenv(_ENV_VAR, raw)
    # Конфиг задаёт ПРОТИВОПОЛОЖНОЕ, чтобы доказать приоритет env.
    cfg = _write_yaml(
        tmp_path, f"claude:\n  require_user_key: {str(not expected).lower()}\n"
    )
    settings = Settings.from_yaml(cfg)

    assert settings.require_user_key is expected


def test_env_takes_priority_over_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Env всегда важнее конфига в обе стороны."""
    monkeypatch.setenv(_ENV_VAR, "true")
    cfg_false = _write_yaml(tmp_path, "claude:\n  require_user_key: false\n")
    assert Settings.from_yaml(cfg_false).require_user_key is True
