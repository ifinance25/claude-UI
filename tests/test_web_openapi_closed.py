"""Схема API не отдаётся анонимам.

`/api/openapi.json` висел открытым: без единой куки он перечислял все
эндпоинты сервиса вместе с параметрами — готовая карта поверхности для
любого, кто открыл адрес инсталляции. Swagger и ReDoc были выключены, а
схема — нет. Фронт её не читает, поэтому схема выключена целиком.
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
    proj = tmp_dir / "alpha"
    proj.mkdir()
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    srv = WebServer(
        settings=settings, allowed_user_ids=[100], bot_username="t",
        session_manager=sm, event_bus=bus, bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[proj],
    )
    try:
        yield srv
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_openapi_schema_is_not_public(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/openapi.json")
    assert r.status_code == 404, f"схема отдаётся анониму: {r.text[:200]}"


async def test_app_declares_no_openapi_url(server):
    # Прямая проверка конфигурации: даже если FastAPI сменит адрес схемы по
    # умолчанию, она останется выключенной.
    assert server.app.openapi_url is None
    assert server.app.docs_url is None
