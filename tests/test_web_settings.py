"""Tests for /api/settings (GET, PATCH)."""
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
def setup(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    (tmp_dir / "demo").mkdir()
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    server = WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
        project_paths=[tmp_dir / "demo"],
    )
    try:
        yield server
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


async def test_settings_requires_auth(setup):
    server = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/settings")
    assert resp.status_code == 401


async def test_settings_default_when_no_sessions(setup):
    server = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json() == {"verbose_level": 1}


async def test_settings_returns_user_level(setup):
    """GET reflects the user-level default stored on the users row."""
    server = setup
    sm = server.session_manager
    sm.set_user_verbose(100, 3)

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/settings")
    assert resp.status_code == 200
    assert resp.json() == {"verbose_level": 3}


async def test_settings_patch_preserves_per_session_overrides(setup):
    """PATCH writes the user-level default but leaves per-session levels alone.

    Per-session ``sessions.verbose_level`` is set via the bot's
    ``/verbose`` command on a specific topic; the user-level web
    setting must not overwrite it (otherwise web would silently undo
    deliberate per-session tweaks).
    """
    server = setup
    sm = server.session_manager
    sm.create_session(
        topic_id=20, project_path=str(server.project_paths[0]),
        project_name="demo", chat_id=100,
    )
    sm.create_session(
        topic_id=21, project_path=str(server.project_paths[0]),
        project_name="demo", chat_id=100,
    )
    # And a session for a different user — must stay untouched.
    sm.create_session(
        topic_id=22, project_path=str(server.project_paths[0]),
        project_name="demo", chat_id=999,
    )
    # Per-session override the user set via the bot's /verbose command.
    sm.set_verbose_level(20, 3)

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.patch("/api/settings", json={"verbose_level": 2})
        assert resp.status_code == 200
        assert resp.json() == {"verbose_level": 2}

        # GET is symmetric with PATCH — reads the same user-level value.
        get_resp = await client.get("/api/settings")
        assert get_resp.json() == {"verbose_level": 2}

    # User-level default persisted.
    assert sm.get_user_default_verbose(100) == 2
    # Per-session overrides preserved (no cascade).
    assert sm.get_session(20).verbose_level == 3
    assert sm.get_session(21).verbose_level == 1
    # Other user's default untouched.
    assert sm.get_user_default_verbose(999) == 1


async def test_settings_patch_rejects_invalid_level(setup):
    server = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        for bad in (-1, 4, 99):
            resp = await client.patch(
                "/api/settings", json={"verbose_level": bad}
            )
            assert resp.status_code == 422, f"expected 422 for level={bad}"
