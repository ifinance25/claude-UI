"""Tests for /api/auth/login (username/password)."""
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
        project_paths=[tmp_dir],
    )
    try:
        yield server, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_password_login_sets_cookie_and_me(setup):
    server, sm = setup
    sm.create_local_user(
        username="alice", password_hash=hash_password("pw123456"), is_admin=False
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "pw123456"}
        )
        assert r.status_code == 200
        assert r.json()["user"]["username"] == "alice"
        me = await client.get("/api/me")
        assert me.status_code == 200
        assert me.json()["is_admin"] is False


async def test_password_login_admin_flag(setup):
    server, sm = setup
    sm.create_local_user(
        username="root", password_hash=hash_password("pw123456"), is_admin=True
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login", json={"username": "root", "password": "pw123456"}
        )
        assert r.status_code == 200
        assert r.json()["user"]["is_admin"] is True
        me = await client.get("/api/me")
        assert me.json()["is_admin"] is True


async def test_password_login_rejects_wrong(setup):
    server, sm = setup
    sm.create_local_user(
        username="bob", password_hash=hash_password("right-pw"), is_admin=False
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login", json={"username": "bob", "password": "nope"}
        )
        assert r.status_code == 401


async def test_password_login_rejects_inactive(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="carol", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_user_active(uid, False)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login", json={"username": "carol", "password": "pw123456"}
        )
        assert r.status_code == 403


async def test_login_rate_limit_blocks_after_max_fails(setup):
    """L-11: серия неудачных входов → 429 + Retry-After (endpoint-wiring лимитера)."""
    server, sm = setup
    sm.create_local_user(
        username="rl", password_hash=hash_password("right-pw"), is_admin=False
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        last = None
        for _ in range(9):  # max_fails=8 → 9-й заблокирован
            last = await client.post(
                "/api/auth/login", json={"username": "rl", "password": "wrong"}
            )
        assert last.status_code == 429
        assert "retry-after" in {k.lower() for k in last.headers}


async def test_login_unknown_user_constant_work(setup, monkeypatch):
    """L-12: для несуществующего логина verify_password всё равно вызывается
    (против фиктивного хэша) — нет тайминг-оракула перечисления логинов."""
    import src.web.routes_auth as ra

    calls: list[str] = []
    real = ra.verify_password

    def spy(pw, h):
        calls.append(h)
        return real(pw, h)

    monkeypatch.setattr(ra, "verify_password", spy)
    server, sm = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/login", json={"username": "ghost", "password": "x"}
        )
    assert r.status_code == 401
    assert calls == [ra._TIMING_DUMMY_HASH]  # ровно один verify против dummy
