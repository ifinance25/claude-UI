"""/api/auth/config: страница входа узнаёт, подключён ли Telegram.

В light бот необязателен — установщик разрешает пропустить токен, и тогда
systemd поднимает только веб (scripts/run_web.py). Страница входа обязана
молчать про Telegram в такой установке, но пользователя на ней ещё нет, значит
/api/me недоступен: признак отдаётся отдельным публичным эндпоинтом.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.server import WebServer


def _make_server(tmp: Path, sm: SessionManager, *, token: str, username: str) -> WebServer:
    return WebServer(
        settings=WebSettings(
            enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
            telegram_bot_username=username,
        ),
        allowed_user_ids=[100],
        bot_username=username,
        session_manager=sm,
        event_bus=EventBus(),
        bot_token=token,
        jwt_secret="x" * 32,
        project_paths=[tmp],
    )


@pytest.fixture
def workdir():
    tmp = Path(tempfile.mkdtemp())
    sm = SessionManager(storage_path=tmp / "sessions.db")
    try:
        yield tmp, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


async def _get_config(server: WebServer) -> dict:
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Без cookie: эндпоинт читают до входа.
        resp = await c.get("/api/auth/config")
        assert resp.status_code == 200
        return resp.json()


async def test_config_reports_bot_when_configured(workdir):
    tmp, sm = workdir
    server = _make_server(tmp, sm, token="12345:abc", username="velsbot")
    body = await _get_config(server)
    assert body["telegram_enabled"] is True
    assert body["telegram_bot_username"] == "velsbot"


async def test_config_reports_no_bot_in_web_only_install(workdir):
    tmp, sm = workdir
    server = _make_server(tmp, sm, token="", username="")
    body = await _get_config(server)
    assert body["telegram_enabled"] is False
    assert body["telegram_bot_username"] == ""


async def test_config_hides_stale_username_without_token(workdir):
    """Осиротевший TELEGRAM_BOT_USERNAME в .env — не повод обещать бота.

    Токен убрали, username остался: бота никто не запускает, /weblogin
    обрабатывать некому — страница входа не должна на него ссылаться.
    """
    tmp, sm = workdir
    server = _make_server(tmp, sm, token="", username="velsbot")
    body = await _get_config(server)
    assert body["telegram_enabled"] is False
    assert body["telegram_bot_username"] == ""
