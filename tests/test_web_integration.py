"""Integration tests for web subsystem wiring into the bot."""
from __future__ import annotations

import pytest

from src.config.settings import Settings, WebSettings
from src.web.server import WebServer


def test_web_disabled_by_default():
    settings = Settings()
    assert settings.web.enabled is False
    assert settings.web.port == 8765
    assert settings.web.host == "127.0.0.1"


async def test_web_server_does_not_start_when_disabled():
    settings = WebSettings(enabled=False)
    server = WebServer(
        settings=settings,
        allowed_user_ids=[1],
        bot_username="x",
        session_manager=None,
        event_bus=None,
    )
    await server.start()
    assert server._uvicorn_server is None
    await server.stop()
