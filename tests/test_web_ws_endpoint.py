"""Tests for the WS /api/ws/sessions/:session_uuid endpoint."""
from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.event_bus.events import AgentStreamingUpdate
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


def _make_server(bus: EventBus, sm: SessionManager) -> WebServer:
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    return WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
    )


def test_ws_requires_auth(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    server = _make_server(bus, sm)

    try:
        with TestClient(server.app) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/api/ws/sessions/anyuuid"):
                    pass
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_ws_streams_events(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    # Create a session so the WS handler can resolve it
    session = sm.create_session(
        topic_id=-1, project_path="/tmp/p", project_name="p", chat_id=100
    )
    server = _make_server(bus, sm)

    try:
        with TestClient(server.app) as client:
            login = _sign(
                "12345:abc",
                {"id": 100, "first_name": "A", "auth_date": int(time.time())},
            )
            resp = client.post("/api/auth/telegram", json=login)
            assert resp.status_code == 200

            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                client.portal.call(
                    bus.publish,
                    AgentStreamingUpdate(
                        request_id="r",
                        chat_id=100,
                        topic_id=session.topic_id,
                        session_uuid=session.session_uuid,
                        kind="text",
                        content="hello",
                    ),
                )

                data = ws.receive_json()
                assert data["type"] == "streaming_update"
                assert data["content"] == "hello"
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
