"""Сессии без проекта (Task 9, C5-backend): создание + listing + WS.

Босс хочет «чистого Claude» без выбора проекта. POST /api/sessions с
``project_path: null`` создаёт валидную сессию без проекта (project_path
сериализуется как "" — единый источник правды с COALESCE в session.py),
а WS-путь не гоняет resolve_project_access (нет проекта → allow, readonly=
False) и запускает Claude в выделенном scratch-каталоге.
"""
from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus, SubscriberPriority
from src.event_bus.events import AgentStarted, UserMessageReceived
from src.web.passwords import hash_password
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


def _make_server(tmp_dir: Path, bus: EventBus, sm: SessionManager) -> WebServer:
    (tmp_dir / "demo").mkdir(exist_ok=True)
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
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
        project_paths=[tmp_dir / "demo"],
        scratch_dir=tmp_dir / "scratch",
    )


@pytest.fixture
def server(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    srv = _make_server(tmp_dir, bus, sm)
    try:
        yield srv
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


async def test_create_session_without_project(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)

        resp = await client.post(
            "/api/sessions", json={"project_path": None, "project_name": None}
        )
        assert resp.status_code == 201
        data = resp.json()
        # project_path сериализуется как "" (а не None) — единый канон с
        # COALESCE(projects.abspath, '') в session.py; фронт/groupByDate не
        # должны спотыкаться на None.
        assert (data.get("project_path") or "") == ""
        assert (data.get("project_name") or "") == ""

        lst = await client.get("/api/sessions")
        assert lst.status_code == 200
        assert any(
            s["session_uuid"] == data["session_uuid"] for s in lst.json()
        )


async def test_create_session_empty_string_project(server):
    """Пустая строка project_path трактуется как «без проекта», не как путь."""
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.post(
            "/api/sessions", json={"project_path": "", "project_name": ""}
        )
        assert resp.status_code == 201
        assert (resp.json().get("project_path") or "") == ""


async def test_no_project_session_file_download_403(server):
    """У сессии без проекта нет скачиваемых файлов — /file отдаёт явный 403,
    а не полагается на побочный отказ resolve_project_access("")."""
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        create = await client.post(
            "/api/sessions", json={"project_path": None, "project_name": None}
        )
        assert create.status_code == 201
        uuid_ = create.json()["session_uuid"]

        resp = await client.get(
            f"/api/sessions/{uuid_}/file", params={"path": "anything.txt"}
        )
        assert resp.status_code == 403


class _CapturingRelay:
    """Ловит UserMessageReceived и публикует AgentStarted, чтобы WS-клиент
    увидел старт генерации (access НЕ был отклонён). Запоминает project_path,
    который ушёл в bus, — он должен указывать на scratch-каталог."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.events: list[UserMessageReceived] = []
        self._unsub = bus.subscribe(
            UserMessageReceived, self._handle, priority=SubscriberPriority.LOW
        )

    def close(self) -> None:
        self._unsub()

    async def _handle(self, event: UserMessageReceived) -> None:
        self.events.append(event)
        await self.bus.publish(
            AgentStarted(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
            )
        )


def _drain_until(ws, target_type: str) -> list[dict]:
    frames: list[dict] = []
    for _ in range(200):
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") == target_type:
            return frames
    raise AssertionError(
        f"did not receive {target_type!r}; got {[f.get('type') for f in frames]}"
    )


def test_ws_no_project_session_runs_in_scratch(tmp_dir):
    """WS к сессии без проекта: access не отклонён, генерация стартует,
    project_path в bus = scratch-каталог."""
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    scratch = tmp_dir / "scratch"
    # Сессия без проекта: project_path="" (как сделает REST-хендлер).
    session = sm.create_web_session("", "", chat_id=100)
    (tmp_dir / "demo").mkdir(exist_ok=True)
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
        project_paths=[tmp_dir / "demo"],
        scratch_dir=scratch,
    )
    relay = _CapturingRelay(bus)
    try:
        with TestClient(server.app) as client:
            login = _sign(
                "12345:abc",
                {"id": 100, "first_name": "A", "auth_date": int(time.time())},
            )
            assert client.post("/api/auth/telegram", json=login).status_code == 200
            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                ws.send_json({"type": "user_message", "text": "hi"})
                frames = _drain_until(ws, "agent_started")
                assert any(f["type"] == "agent_started" for f in frames)

        assert len(relay.events) == 1
        evt = relay.events[0]
        # Claude должен запускаться в scratch-каталоге, не в "" / cwd.
        assert Path(evt.project_path).resolve() == scratch.resolve()
        assert evt.readonly is False
        # scratch создан лениво в момент использования.
        assert scratch.is_dir()
    finally:
        relay.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_ws_no_project_nonpriv_confined_to_scratch(tmp_dir):
    """CRITICAL: непривилегированный юзер в сессии без проекта confine'ится в
    scratch. Иначе Read/Grep/Glob под bypassPermissions читали бы .env /
    ~/.claude/.credentials.json / чужие проекты (firewall не был активен, т.к.
    confine_root оставался None).

    Непривилегированный, но логинящийся актор: ЛОКАЛЬНЫЙ аккаунт (origin=
    'local', id ≥ 1_000_000_001) — он НЕ в whitelist [100] и НЕ admin, но
    входит по логину/паролю через /api/auth/login (Telegram-вход для
    не-whitelist id невозможен). Сессия без проекта создаётся на его chat_id.
    """
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    scratch = tmp_dir / "scratch"
    local_uid = sm.create_local_user(
        username="bob", password_hash=hash_password("pw-secret-123"), is_admin=False
    )
    session = sm.create_web_session("", "", chat_id=local_uid)
    server = _make_server(tmp_dir, bus, sm)
    relay = _CapturingRelay(bus)
    try:
        with TestClient(server.app) as client:
            resp = client.post(
                "/api/auth/login",
                json={"username": "bob", "password": "pw-secret-123"},
            )
            assert resp.status_code == 200
            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                ws.send_json({"type": "user_message", "text": "hi"})
                frames = _drain_until(ws, "agent_started")
                assert any(f["type"] == "agent_started" for f in frames)

        assert len(relay.events) == 1
        evt = relay.events[0]
        # Непривилегированный → readonly и confine в scratch (firewall активен).
        assert evt.readonly is True
        assert evt.confine_root == str(scratch.resolve())
        assert Path(evt.project_path).resolve() == scratch.resolve()
    finally:
        relay.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_ws_no_project_privileged_not_confined(tmp_dir):
    """Привилегированный (whitelist) юзер в сессии без проекта: полный доступ
    (readonly=False) и БЕЗ confine (confine_root is None) — доверенный оператор."""
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    session = sm.create_web_session("", "", chat_id=100)
    server = _make_server(tmp_dir, bus, sm)
    relay = _CapturingRelay(bus)
    try:
        with TestClient(server.app) as client:
            login = _sign(
                "12345:abc",
                {"id": 100, "first_name": "A", "auth_date": int(time.time())},
            )
            assert client.post("/api/auth/telegram", json=login).status_code == 200
            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                ws.send_json({"type": "user_message", "text": "hi"})
                frames = _drain_until(ws, "agent_started")
                assert any(f["type"] == "agent_started" for f in frames)

        assert len(relay.events) == 1
        evt = relay.events[0]
        assert evt.readonly is False
        assert evt.confine_root is None
    finally:
        relay.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()
