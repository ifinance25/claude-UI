"""L-2: явный ``OWNER_USER_ID`` для промоута владельца в админы.

Приоритет: env ``OWNER_USER_ID`` > первый из ``ALLOWED_USER_IDS``. Установщик
пишет в .env id, который оператор указал как СВОЙ, чтобы админом не стал
случайно вписанный раньше ученик.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config.settings import Settings

_ENV_OWNER = "OWNER_USER_ID"
_ENV_ALLOWED = "ALLOWED_USER_IDS"


def _write_yaml(tmp_path: Path, body: str = "claude:\n  transport: sdk\n") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_owner_from_env_takes_priority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_ENV_OWNER, "111")
    monkeypatch.setenv(_ENV_ALLOWED, "222,333")
    settings = Settings.from_yaml(_write_yaml(tmp_path))
    assert settings.get_owner_user_id() == 111


def test_falls_back_to_first_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(_ENV_OWNER, raising=False)
    monkeypatch.setenv(_ENV_ALLOWED, "222,333")
    settings = Settings.from_yaml(_write_yaml(tmp_path))
    assert settings.get_owner_user_id() == 222


def test_none_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Пустой ALLOWED перекрывает возможный .env репозитория; OWNER не задан.
    monkeypatch.delenv(_ENV_OWNER, raising=False)
    monkeypatch.setenv(_ENV_ALLOWED, "")
    settings = Settings.from_yaml(_write_yaml(tmp_path))
    assert settings.get_owner_user_id() is None


def test_invalid_owner_env_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_ENV_OWNER, "not-a-number")
    monkeypatch.setenv(_ENV_ALLOWED, "444")
    settings = Settings.from_yaml(_write_yaml(tmp_path))
    assert settings.get_owner_user_id() == 444
