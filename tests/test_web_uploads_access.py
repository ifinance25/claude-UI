"""POST /api/uploads обязан уважать уровень доступа (read-only = нет записи).

Остаточная находка перепроверки: upload гейтился только на владение сессией,
без resolve_project_access — read-only юзер мог писать файл в проект
(<project>/.claude/uploads/), обходя read-only-контракт.
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
        project_paths=[tmp_dir / "alpha"],
    )
    try:
        yield server, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def _login(client, u, p):
    r = await client.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text


_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


async def test_readonly_user_cannot_upload(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="ro", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "readonly")
    sess = sm.create_web_session(str(server.project_paths[0]), "alpha", uid)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "ro", "pw123456")
        r = await client.post(
            "/api/uploads",
            data={"session_uuid": sess.session_uuid},
            files={"file": ("a.png", _PNG, "image/png")},
        )
    assert r.status_code == 403, r.text
    # Файл не должен появиться на диске.
    assert not (server.project_paths[0] / ".claude" / "uploads").exists()


async def test_upload_over_limit_rejected(setup, monkeypatch):
    """L-10: тело больше лимита → 413, файл не сохранён (потоковый ранний обрыв)."""
    import src.web.routes_uploads as ru

    monkeypatch.setattr(ru, "MAX_UPLOAD_BYTES", 1024)
    server, sm = setup
    uid = sm.create_local_user(
        username="rw", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")
    sess = sm.create_web_session(str(server.project_paths[0]), "alpha", uid)
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096  # > 1024
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "rw", "pw123456")
        r = await client.post(
            "/api/uploads",
            data={"session_uuid": sess.session_uuid},
            files={"file": ("big.png", big, "image/png")},
        )
    assert r.status_code == 413, r.text
    assert not (server.project_paths[0] / ".claude" / "uploads" / "big.png").exists()


async def test_full_user_can_upload(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="rw", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(server.project_paths[0]), "full")
    sess = sm.create_web_session(str(server.project_paths[0]), "alpha", uid)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client, "rw", "pw123456")
        r = await client.post(
            "/api/uploads",
            data={"session_uuid": sess.session_uuid},
            files={"file": ("a.png", _PNG, "image/png")},
        )
    assert r.status_code == 200, r.text
    assert r.json()["source_path"].endswith("a.png")
