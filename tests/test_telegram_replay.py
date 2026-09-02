"""L-1: подписанный Telegram-login payload одноразовый — перехваченный
(из логов/Referer) нельзя проиграть повторно за новый JWT."""
from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.server import WebServer


@pytest.fixture
def setup():
    tmp = Path(tempfile.mkdtemp())
    bus = EventBus()
    sm = SessionManager(storage_path=tmp / "s.db")
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    server = WebServer(
        settings=settings, allowed_user_ids=[100], bot_username="t",
        session_manager=sm, event_bus=bus, bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[tmp],
    )
    try:
        yield server
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def _sign(bot_token: str, payload: dict) -> dict:
    secret = hashlib.sha256(bot_token.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


async def test_telegram_login_payload_is_single_use(setup):
    server = setup
    payload = _sign(
        "12345:abc",
        {"id": 100, "first_name": "A", "auth_date": int(time.time())},
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r1 = await c.post("/api/auth/telegram", json=payload)
        assert r1.status_code == 200, r1.text
    # Повторное использование того же подписанного payload — отказ (replay).
    async with AsyncClient(transport=transport, base_url="http://test") as c2:
        r2 = await c2.post("/api/auth/telegram", json=payload)
        assert r2.status_code == 401
