"""WS-слой: лимит соединений (M-5), отзыв доступа mid-stream и на handshake
(M-4 / H-2-handshake), и no-project readonly для локального non-admin (C-1).

Этот путь запускает Claude с bypassPermissions, поэтому покрытие критично.
"""
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.event_bus.events import UserMessageReceived
from src.web.passwords import hash_password
from src.web.server import WebServer


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _server(tmp_dir, sm, bus):
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    return WebServer(
        settings=settings, allowed_user_ids=[], bot_username="t",
        session_manager=sm, event_bus=bus, bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[tmp_dir],
    )


def _login_local(client, sm, username="u", admin=False):
    uid = sm.create_local_user(
        username=username, password_hash=hash_password("pw1234567890"), is_admin=admin
    )
    r = client.post("/api/auth/login", json={"username": username, "password": "pw1234567890"})
    assert r.status_code == 200
    return uid


def test_ws_connection_cap_per_user(tmp_dir):
    """M-5: 8 каналов на юзера ОК, 9-й рвётся (1008); закрыл один → снова можно."""
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "s.db")
    server = _server(tmp_dir, sm, bus)
    try:
        with TestClient(server.app) as client:
            uid = _login_local(client, sm)
            sess = sm.create_web_session("", "", uid)
            url = f"/api/ws/sessions/{sess.session_uuid}"
            with contextlib.ExitStack() as stack:
                for _ in range(8):  # MAX_WS_PER_USER = 8
                    stack.enter_context(client.websocket_connect(url))
                # 9-й сверх лимита — сервер закрывает до accept.
                with pytest.raises(WebSocketDisconnect):
                    with client.websocket_connect(url):
                        pass
            # ExitStack закрыл все 8 → слоты освободились, новый канал открывается.
            with client.websocket_connect(url):
                pass
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_ws_handshake_revoked_for_deactivated_owner(tmp_dir):
    """H-2/handshake: деактивированный владелец своей сессии → 1008 на connect."""
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "s.db")
    server = _server(tmp_dir, sm, bus)
    try:
        with TestClient(server.app) as client:
            uid = _login_local(client, sm)
            sess = sm.create_web_session("", "", uid)
            sm.set_user_active(uid, False)  # отключили после логина
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/api/ws/sessions/{sess.session_uuid}"):
                    pass
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_ws_mid_stream_revocation_blocks_user_message(tmp_dir):
    """M-4: отзыв посреди открытого канала → следующий user_message не запускает
    Claude, клиент получает кадр error."""
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "s.db")
    server = _server(tmp_dir, sm, bus)
    started: list = []
    bus.subscribe(UserMessageReceived, lambda e: started.append(e))
    try:
        with TestClient(server.app) as client:
            uid = _login_local(client, sm)
            sess = sm.create_web_session("", "", uid)
            with client.websocket_connect(f"/api/ws/sessions/{sess.session_uuid}") as ws:
                sm.set_user_active(uid, False)  # отзыв при открытом канале
                ws.send_json({"type": "user_message", "text": "hi"})
                data = ws.receive_json()
                assert data["type"] == "error"
                assert "disabled" in data["error"] or "removed" in data["error"]
        assert started == []  # генерация не запускалась
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_ws_no_project_readonly_for_local_non_admin(tmp_dir):
    """C-1: локальный non-admin в no-project сессии → Claude запускается readonly
    (UserMessageReceived.readonly=True), а не с полным доступом."""
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "s.db")
    server = _server(tmp_dir, sm, bus)
    captured: list = []
    bus.subscribe(UserMessageReceived, lambda e: captured.append(e))
    try:
        with TestClient(server.app) as client:
            uid = _login_local(client, sm, admin=False)
            sess = sm.create_web_session("", "", uid)  # no-project
            with client.websocket_connect(f"/api/ws/sessions/{sess.session_uuid}") as ws:
                ws.send_json({"type": "user_message", "text": "hi"})
                ws.receive_json()  # дождаться обработки (любой кадр/завершение)
        assert captured, "UserMessageReceived должен был опубликоваться"
        assert captured[0].readonly is True
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
