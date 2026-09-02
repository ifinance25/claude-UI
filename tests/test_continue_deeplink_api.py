"""/api/me отдаёт telegram_bot_username (для кнопки «Продолжить в Telegram»)."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.passwords import hash_password
from src.web.server import WebServer


@pytest.fixture
def setup():
    tmp = Path(tempfile.mkdtemp())
    sm = SessionManager(storage_path=tmp / "sessions.db")
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="velsbot",
    )
    server = WebServer(
        settings=settings, allowed_user_ids=[100], bot_username="velsbot",
        session_manager=sm, event_bus=EventBus(), bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[tmp],
    )
    try:
        yield server, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


async def test_me_returns_bot_username(setup):
    server, sm = setup
    sm.create_local_user(username="u", password_hash=hash_password("pw123456"), is_admin=False)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "u", "password": "pw123456"})
        me = await c.get("/api/me")
        assert me.status_code == 200
        assert me.json()["telegram_bot_username"] == "velsbot"
        # BD4: локальный (логин/пароль) юзер не может продолжить в Telegram —
        # кнопка должна быть скрыта (owner-check у deeplink не совпадёт).
        assert me.json()["can_continue_in_telegram"] is False
