"""GET /api/docs/guide — статический пользовательский гайд (Task 13).

Гайд НЕ зависит от выбранного проекта: один и тот же markdown для всех
сессий, включая сессии без проекта. Эндпойнт требует обычной авторизации
(как остальные /api/docs).
"""
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


def _sign(bot_token: str, payload: dict) -> dict:
    secret = hashlib.sha256(bot_token.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def server(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    (tmp_dir / "demo").mkdir(exist_ok=True)
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    srv = WebServer(
        settings=settings, allowed_user_ids=[100], bot_username="t",
        session_manager=sm, event_bus=bus, bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[tmp_dir / "demo"],
    )
    try:
        yield srv
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def _login(client: AsyncClient) -> None:
    payload = _sign(
        "12345:abc",
        {"id": 100, "first_name": "A", "auth_date": int(time.time())},
    )
    resp = await client.post("/api/auth/telegram", json=payload)
    assert resp.status_code == 200


async def test_docs_guide_returns_markdown(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/docs/guide")
    assert resp.status_code == 200
    data = resp.json()
    assert "content" in data and "Vels Claude" in data["content"]


async def test_docs_guide_requires_auth(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/docs/guide")
    assert resp.status_code == 401
