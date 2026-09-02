"""Tests for per-user project access filtering on /api/projects."""
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
    (tmp_dir / "alpha").mkdir()
    (tmp_dir / "beta").mkdir()
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
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
        project_paths=[tmp_dir / "alpha", tmp_dir / "beta"],
    )
    try:
        yield server, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_local_user_sees_only_granted(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")  # alpha
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/login", json={"username": "u1", "password": "pw123456"}
        )
        r = await client.get("/api/projects")
    assert r.status_code == 200
    paths = {p["path"] for p in r.json()}
    assert paths == {str(server.project_paths[0])}


async def test_admin_sees_all(setup):
    server, sm = setup
    sm.create_local_user(
        username="root", password_hash=hash_password("pw123456"), is_admin=True
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/login", json={"username": "root", "password": "pw123456"}
        )
        r = await client.get("/api/projects")
    assert r.status_code == 200
    assert len(r.json()) == 2
