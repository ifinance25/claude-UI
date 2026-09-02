"""End-to-end security test: user A must not see/touch user B's sessions.

Exercises every route added or modified by the security pass so a future
regression resurfacing the "no chat_id check" bug fails CI.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.server import WebServer

BOT_TOKEN = "12345:abc"
USER_A = 100
USER_B = 200


def _sign(payload: dict) -> dict:
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


async def _login(client: AsyncClient, user_id: int) -> None:
    payload = _sign(
        {"id": user_id, "first_name": "X", "auth_date": int(time.time())}
    )
    resp = await client.post("/api/auth/telegram", json=payload)
    assert resp.status_code == 200, resp.text


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
    (tmp_dir / "demo").mkdir()
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0,
        jwt_secret="x" * 32, telegram_bot_username="t",
    )
    srv = WebServer(
        settings=settings,
        allowed_user_ids=[USER_A, USER_B],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token=BOT_TOKEN,
        jwt_secret="x" * 32,
        project_paths=[tmp_dir / "demo"],
    )
    try:
        yield srv
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_list_sessions_scoped_to_owner(server):
    """GET /api/sessions returns only the caller's sessions."""
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://t") as ca, \
               AsyncClient(transport=transport, base_url="http://t") as cb:
        await _login(ca, USER_A)
        await _login(cb, USER_B)

        # A creates a session
        r = await ca.post("/api/sessions", json={
            "project_path": str(server.project_paths[0]),
            "project_name": "demo",
        })
        assert r.status_code == 201
        uuid_a = r.json()["session_uuid"]

        # B sees nothing
        r = await cb.get("/api/sessions")
        assert r.status_code == 200
        assert r.json() == []

        # A sees their own
        r = await ca.get("/api/sessions")
        assert {s["session_uuid"] for s in r.json()} == {uuid_a}


async def test_cross_user_delete_returns_404(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://t") as ca, \
               AsyncClient(transport=transport, base_url="http://t") as cb:
        await _login(ca, USER_A)
        await _login(cb, USER_B)

        r = await ca.post("/api/sessions", json={
            "project_path": str(server.project_paths[0]),
            "project_name": "demo",
        })
        uuid_a = r.json()["session_uuid"]

        # B's DELETE → 404, A's session still alive
        r = await cb.delete(f"/api/sessions/{uuid_a}")
        assert r.status_code == 404

        r = await ca.get("/api/sessions")
        assert any(s["session_uuid"] == uuid_a for s in r.json())


async def test_cross_user_get_messages_returns_404(server):
    transport = ASGITransport(app=server.app)
    sm = server.session_manager
    async with AsyncClient(transport=transport, base_url="http://t") as ca, \
               AsyncClient(transport=transport, base_url="http://t") as cb:
        await _login(ca, USER_A)
        await _login(cb, USER_B)

        r = await ca.post("/api/sessions", json={
            "project_path": str(server.project_paths[0]),
            "project_name": "demo",
        })
        uuid_a = r.json()["session_uuid"]
        topic_a = r.json()["topic_id"]

        # Plant a message into A's session directly
        conn = sm._get_connection()
        conn.execute(
            "INSERT INTO messages (topic_id, type, kind, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (topic_a, "user_message", None, "secret-content-of-A"),
        )
        conn.commit()

        # B's GET → 404 (must NOT leak content)
        r = await cb.get(f"/api/sessions/{uuid_a}/messages")
        assert r.status_code == 404
        assert "secret-content-of-A" not in r.text

        # A's GET → 200 with their message
        r = await ca.get(f"/api/sessions/{uuid_a}/messages")
        assert r.status_code == 200
        assert any(m["content"] == "secret-content-of-A" for m in r.json())


async def test_cross_user_patch_empty_body_returns_404(server):
    """Regression: PATCH with empty body used to leak the session via
    a final _resolve_session() call after the owner check was skipped."""
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://t") as ca, \
               AsyncClient(transport=transport, base_url="http://t") as cb:
        await _login(ca, USER_A)
        await _login(cb, USER_B)

        r = await ca.post("/api/sessions", json={
            "project_path": str(server.project_paths[0]),
            "project_name": "demo",
        })
        uuid_a = r.json()["session_uuid"]

        # B's PATCH with no payload fields → 404, no body leakage
        r = await cb.patch(f"/api/sessions/{uuid_a}", json={})
        assert r.status_code == 404
        assert "demo" not in r.text or "session not found" in r.text


def test_cross_user_ws_rejected(server):
    """WS handshake must close 1008 when the caller doesn't own the session.

    Uses Starlette's TestClient because httpx has no WS support; both
    drive the FastAPI app via ASGI.
    """
    sm = server.session_manager
    # Seed: a session owned by A
    sess = sm.create_session(
        topic_id=-50,
        project_path=str(server.project_paths[0]),
        project_name="demo",
        chat_id=USER_A,
    )
    uuid_a = sess.session_uuid

    with TestClient(server.app) as client:
        # B logs in via HTTP first (TestClient persists cookies)
        payload = _sign(
            {"id": USER_B, "first_name": "X", "auth_date": int(time.time())}
        )
        r = client.post("/api/auth/telegram", json=payload)
        assert r.status_code == 200

        # Now B tries to open A's WS — must be refused (1008).
        from starlette.websockets import WebSocketDisconnect
        try:
            with client.websocket_connect(f"/api/ws/sessions/{uuid_a}") as ws:
                # If we get here without disconnect, the security check is broken.
                ws.send_text(json.dumps({"type": "user_message", "text": "x"}))
                # Drain or assert disconnect on receive
                msg = ws.receive()
                pytest.fail(f"WS should have been rejected, got {msg}")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008
