"""WS обязан вычислять readonly через единый resolver (deny-by-default).

Регрессия: readonly резолвился точным строковым сравнением project_path, и
вариант пути (трейлинг-слэш / под-каталог) давал None → readonly=False →
обход read-only и запись/исполнение. Теперь readonly считается по
нормализованному пути через resolve_project_access.
"""
from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

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


def _server(bus, sm, project_paths):
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    return WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
        project_paths=project_paths,
    )


def _capture_readonly_for(tmp_dir, *, stored_project_path: str, access_level: str):
    """Поднимает сервер, логинит readonly/full-юзера с грантом на alpha,
    создаёт его сессию с stored_project_path, шлёт WS user_message и
    возвращает значение readonly из опубликованного UserMessageReceived."""
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    alpha = tmp_dir / "alpha"
    alpha.mkdir()
    server = _server(bus, sm, [alpha])

    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    sm.set_project_access(uid, str(alpha), access_level)
    session = sm.create_web_session(stored_project_path, "alpha", uid)

    captured: list[bool] = []

    async def _rec(ev: UserMessageReceived) -> None:
        captured.append(ev.readonly)

    bus.subscribe(UserMessageReceived, _rec)

    try:
        with TestClient(server.app) as client:
            r = client.post(
                "/api/auth/login", json={"username": "u1", "password": "pw123456"}
            )
            assert r.status_code == 200, r.text
            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                ws.send_json({"type": "user_message", "text": "hi"})
                deadline = time.time() + 5
                while not captured and time.time() < deadline:
                    time.sleep(0.05)
        return captured
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_ws_readonly_grant_survives_trailing_slash_path(tmp_dir):
    captured = _capture_readonly_for(
        tmp_dir, stored_project_path=str(tmp_dir / "alpha") + "/",
        access_level="readonly",
    )
    assert captured == [True]


def test_ws_full_grant_is_not_readonly(tmp_dir):
    captured = _capture_readonly_for(
        tmp_dir, stored_project_path=str(tmp_dir / "alpha"),
        access_level="full",
    )
    assert captured == [False]
