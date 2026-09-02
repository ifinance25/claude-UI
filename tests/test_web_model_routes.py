"""Smoke tests for /api/model and /api/slash-commands."""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.server import WebServer

BOT_TOKEN = "12345:abc"
USER = 100


def _sign(payload: dict) -> dict:
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


async def _login(client: AsyncClient) -> None:
    payload = _sign(
        {"id": USER, "first_name": "X", "auth_date": int(time.time())}
    )
    resp = await client.post("/api/auth/telegram", json=payload)
    assert resp.status_code == 200


def _make_admin(sm, user_id: int) -> None:
    """Помечает пользователя админом в БД (PATCH /api/model теперь admin-only)."""
    with sm._conn_lock:
        conn = sm._get_connection()
        conn.execute(
            "INSERT INTO users (user_id, username, is_admin, is_active) "
            "VALUES (?, 'tg', 1, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET is_admin=1, is_active=1",
            (user_id,),
        )
        conn.commit()


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def server(tmp_dir, monkeypatch):
    # Redirect ~/.claude/settings.json to a temp file so tests can't
    # clobber the developer's real settings.
    fake_settings = tmp_dir / "claude_settings.json"
    monkeypatch.setattr(
        "src.web.routes_model.CLAUDE_SETTINGS_PATH",
        fake_settings,
    )
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    (tmp_dir / "demo").mkdir()
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0,
        jwt_secret="x" * 32, telegram_bot_username="t",
    )
    srv = WebServer(
        settings=settings,
        allowed_user_ids=[USER],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token=BOT_TOKEN,
        jwt_secret="x" * 32,
        project_paths=[tmp_dir / "demo"],
    )
    try:
        yield srv, fake_settings, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_get_model_returns_default_when_settings_absent(server):
    srv, _path, _sm = server
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await _login(c)
        r = await c.get("/api/model")
    assert r.status_code == 200
    body = r.json()
    assert body["current"] == "claude-sonnet-5"
    assert any(m["id"] == "claude-opus-5" for m in body["known"])


async def test_patch_model_writes_settings_and_clears_user_sessions(server):
    srv, path, sm = server
    # Seed: a session owned by USER with a session_id (simulates resumed Claude).
    sess = sm.create_session(
        topic_id=-7, project_path="/x", project_name="x", chat_id=USER
    )
    sm.update_session_id(sess.topic_id, "claude-session-xyz")
    assert sm.get_session(sess.topic_id).session_id == "claude-session-xyz"
    _make_admin(sm, USER)

    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await _login(c)
        r = await c.patch("/api/model", json={"model": "claude-opus-5"})
    assert r.status_code == 200
    assert r.json()["current"] == "claude-opus-5"

    # File written
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "claude-opus-5"

    # Session id cleared so next message starts a fresh Claude session
    assert sm.get_session(sess.topic_id).session_id is None


async def test_patch_model_rejects_empty(server):
    srv, _path, sm = server
    _make_admin(sm, USER)
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await _login(c)
        r = await c.patch("/api/model", json={"model": "  "})
    assert r.status_code == 422


async def test_patch_model_rejects_unknown_model(server):
    """CR3-3: only allowlisted model ids may be written to settings.json."""
    srv, path, sm = server
    _make_admin(sm, USER)
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await _login(c)
        r = await c.patch("/api/model", json={"model": "gpt-4-evil" * 100})
    assert r.status_code == 422
    # The bogus value must never have reached the global settings file.
    assert not path.exists() or "gpt-4-evil" not in path.read_text(encoding="utf-8")


async def test_patch_model_requires_admin(server):
    """Смена ГЛОБАЛЬНОЙ модели — только для админа (не любой залогиненный)."""
    srv, _path, _sm = server  # USER НЕ помечен админом
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await _login(c)
        r = await c.patch("/api/model", json={"model": "claude-opus-5"})
    assert r.status_code == 403


async def test_slash_commands_listed(server):
    srv, _path, _sm = server
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        await _login(c)
        r = await c.get("/api/slash-commands")
    assert r.status_code == 200
    cmds = {c["cmd"] for c in r.json()}
    assert "/model" in cmds
    assert "/mcp" in cmds
    assert "/cost" in cmds


async def test_model_routes_require_auth(server):
    srv, _path, _sm = server
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r1 = await c.get("/api/model")
        r2 = await c.patch("/api/model", json={"model": "opus"})
        r3 = await c.get("/api/slash-commands")
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 401
