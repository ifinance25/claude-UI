"""Tests for WebServer FastAPI skeleton."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.config.settings import WebSettings
from src.web.server import WebServer


async def test_health_endpoint():
    settings = WebSettings(enabled=True, host="127.0.0.1", port=8766)
    server = WebServer(
        settings=settings,
        allowed_user_ids=[1],
        bot_username="testbot",
        session_manager=None,
        event_bus=None,
    )

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_disabled_server_skips_start():
    """When settings.enabled is False, start() is a no-op (no uvicorn instance)."""
    settings = WebSettings(enabled=False)
    server = WebServer(
        settings=settings,
        allowed_user_ids=[1],
        bot_username="testbot",
        session_manager=None,
        event_bus=None,
    )
    await server.start()
    assert server._uvicorn_server is None
    await server.stop()
