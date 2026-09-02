"""GET /api/sessions/{uuid}/file: абсолютный путь внутри корня проекта отдаётся,
путь вне корня — 400.

Кейс перенесён из tests/test_artifacts.py (удалён вместе с файловым менеджером
в light-версии): панель артефактов ушла, а скачивание файла по абсолютному пути
осталось — его шлёт FileArtifactEvent в ленте чата. Проверка границы корня —
единственная в наборе, поэтому живёт отдельным файлом.
"""
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
    (proj / "out.txt").write_text("generated", encoding="utf-8")
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
        yield server, sm, str(proj), proj
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def _login(c: AsyncClient, username: str, password: str) -> None:
    await c.post("/api/auth/login", json={"username": username, "password": password})


async def test_absolute_path_inside_project_is_served(setup):
    server, sm, pp, proj = setup
    uid = sm.create_local_user(
        username="dl", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, pp, "readonly")
    session = sm.create_session(
        topic_id=-1, project_path=pp, project_name="alpha", chat_id=uid
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await _login(c, "dl", "pw123456")
        resp = await c.get(
            f"/api/sessions/{session.session_uuid}/file",
            params={"path": str(proj / "out.txt")},
        )
    assert resp.status_code == 200
    assert resp.content == b"generated"


async def test_absolute_path_outside_project_rejected(setup):
    server, sm, pp, _proj = setup
    uid = sm.create_local_user(
        username="dl2", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, pp, "readonly")
    session = sm.create_session(
        topic_id=-2, project_path=pp, project_name="alpha", chat_id=uid
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await _login(c, "dl2", "pw123456")
        resp = await c.get(
            f"/api/sessions/{session.session_uuid}/file",
            params={"path": "/etc/hosts"},
        )
    assert resp.status_code == 400
