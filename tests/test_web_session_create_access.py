"""POST /api/sessions обязан авторизовать project_path (deny-by-default).

Регрессия мультиюзер-ревью: создание сессии на невыданном/произвольном пути
давало владение + full-доступ к Claude (RCE) и чтение любого файла через
/file. Теперь — 403, если у юзера нет гранта на проект.
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
        yield server, sm, tmp_dir
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def _login(client, username, password):
    r = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text


async def test_local_user_can_create_on_granted_project(setup):
    server, sm, _ = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")  # alpha
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "u1", "pw123456")
        r = await client.post(
            "/api/sessions", json={"project_path": str(server.project_paths[0])}
        )
    assert r.status_code == 201, r.text


async def test_local_user_cannot_create_on_ungranted_project(setup):
    server, sm, _ = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")  # только alpha
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "u1", "pw123456")
        r = await client.post(
            "/api/sessions", json={"project_path": str(server.project_paths[1])}  # beta
        )
    assert r.status_code == 403, r.text


async def test_local_user_cannot_create_on_arbitrary_server_path(setup):
    server, sm, tmp_dir = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "u1", "pw123456")
        # Корень диска / родитель проектов — вне настроенных корней.
        r = await client.post("/api/sessions", json={"project_path": str(tmp_dir)})
    assert r.status_code == 403, r.text


async def test_download_blocked_after_access_revoked(setup):
    """/file перепроверяет ТЕКУЩИЙ доступ, а не только владение сессией."""
    server, sm, _ = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    alpha = server.project_paths[0]
    sm.set_project_access(uid, str(alpha), "full")
    (alpha / "readme.md").write_text("hello", encoding="utf-8")
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "u1", "pw123456")
        r = await client.post("/api/sessions", json={"project_path": str(alpha)})
        uuid = r.json()["session_uuid"]
        r = await client.get(
            f"/api/sessions/{uuid}/file", params={"path": "readme.md"}
        )
        assert r.status_code == 200, r.text  # доступ есть — качаем
        # Админ отзывает доступ — дальше скачивать нельзя.
        sm.revoke_project_access(uid, str(alpha))
        r = await client.get(
            f"/api/sessions/{uuid}/file", params={"path": "readme.md"}
        )
    assert r.status_code == 403, r.text


async def test_admin_can_create_on_any_configured_project(setup):
    server, sm, _ = setup
    sm.create_local_user(
        username="root", password_hash=hash_password("pw123456"), is_admin=True
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "root", "pw123456")
        r = await client.post(
            "/api/sessions", json={"project_path": str(server.project_paths[1])}
        )
    assert r.status_code == 201, r.text


async def test_session_dto_carries_project_id_for_real_project(setup):
    """Сессия на реальном проекте несёт числовой project_id и в POST-ответе,
    и в листинге GET /api/sessions — фронт использует его как ключ
    /api/projects/{project_id}/members."""
    server, sm, _ = setup
    sm.create_local_user(
        username="root", password_hash=hash_password("pw123456"), is_admin=True
    )
    alpha = str(server.project_paths[0])
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "root", "pw123456")
        r = await client.post("/api/sessions", json={"project_path": alpha})
        assert r.status_code == 201, r.text
        created = r.json()
        assert isinstance(created["project_id"], int)

        r = await client.get("/api/sessions")
        assert r.status_code == 200, r.text
        sessions = r.json()
        assert len(sessions) == 1
        assert isinstance(sessions[0]["project_id"], int)
        assert sessions[0]["project_id"] == created["project_id"]


async def test_no_project_session_has_null_project_id(setup):
    """Сессия «без проекта» (scratch) → project_id == null (кнопка «Участники»
    скрыта на фронте)."""
    server, sm, _ = setup
    sm.create_local_user(
        username="root", password_hash=hash_password("pw123456"), is_admin=True
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "root", "pw123456")
        r = await client.post("/api/sessions", json={})
        assert r.status_code == 201, r.text
        assert r.json()["project_id"] is None

        r = await client.get("/api/sessions")
        assert r.status_code == 200, r.text
        sessions = r.json()
        assert len(sessions) == 1
        assert sessions[0]["project_id"] is None
