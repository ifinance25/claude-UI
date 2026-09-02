"""HTTP-тесты доступа к /api/docs по правам пользователя."""
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
    proj = tmp_dir / "alpha"
    proj.mkdir()
    (proj / "README.md").write_text("# Alpha", encoding="utf-8")
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    server = WebServer(
        settings=settings, allowed_user_ids=[100], bot_username="t",
        session_manager=sm, event_bus=bus, bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[proj],
    )
    try:
        yield server, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_local_user_without_grant_denied(setup):
    server, sm = setup
    sm.create_local_user(username="u1", password_hash=hash_password("pw123456"), is_admin=False)
    pp = str(server.project_paths[0])
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "u1", "password": "pw123456"})
        r = await c.get("/api/docs", params={"project_path": pp})
    assert r.status_code == 403


async def test_local_user_with_grant_sees_docs(setup):
    server, sm = setup
    uid = sm.create_local_user(username="u2", password_hash=hash_password("pw123456"), is_admin=False)
    pp = str(server.project_paths[0])
    sm.set_project_access(uid, pp, "readonly")
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "u2", "password": "pw123456"})
        r = await c.get("/api/docs", params={"project_path": pp})
    assert r.status_code == 200
    assert any(d["rel"] == "README.md" for d in r.json())


async def test_admin_sees_docs(setup):
    server, sm = setup
    sm.create_local_user(username="root", password_hash=hash_password("pw123456"), is_admin=True)
    pp = str(server.project_paths[0])
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "root", "password": "pw123456"})
        r = await c.get("/api/docs", params={"project_path": pp})
    assert r.status_code == 200
