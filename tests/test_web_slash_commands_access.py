"""/api/slash-commands обязан авторизовать project_path.

Регрессия: эндпоинт спавнил `claude --dangerously-skip-permissions` с
произвольным cwd и читал <project_path>/.claude/skills/* без проверки
доступа. Теперь — 403 на невыданный путь.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import src.web.routes_model as rm
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


@pytest.fixture(autouse=True)
def _no_cli_spawn(monkeypatch):
    async def fake_fetch(project_path):
        return []

    monkeypatch.setattr(rm, "fetch_cli_commands_for_project", fake_fetch)


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


async def _login(client, u, p):
    r = await client.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text


async def test_granted_project_returns_commands(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "u1", "pw123456")
        r = await client.get(
            "/api/slash-commands",
            params={"project_path": str(server.project_paths[0])},
        )
    assert r.status_code == 200, r.text
    assert any(c["cmd"] == "/model" for c in r.json())


async def test_ungranted_project_forbidden(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "u1", "pw123456")
        r = await client.get(
            "/api/slash-commands",
            params={"project_path": str(server.project_paths[1])},  # beta, нет гранта
        )
    assert r.status_code == 403, r.text


async def test_no_project_path_returns_builtins(setup):
    server, sm = setup
    sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "u1", "pw123456")
        r = await client.get("/api/slash-commands")
    assert r.status_code == 200, r.text
    assert any(c["cmd"] == "/model" for c in r.json())
