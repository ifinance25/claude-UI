"""Уход в другой диалог не обрывает ответ Claude.

Регрессия: закрытие WebSocket (переход в другую сессию, закрытая вкладка)
отменяло фоновую publish-задачу вместе с подпроцессом Claude. Ответ умирал на
середине, в историю не попадало ничего, и вернувшийся пользователь видел свой
вопрос без ответа — при том что смысл сервиса в обратном: задача считается на
сервере, результат читают позже.

Харнесс (login + WS) повторяет tests/web/test_stop_generation.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus, SubscriberPriority
from src.event_bus.events import (
    AgentFinished,
    AgentStarted,
    AgentStreamingUpdate,
    UserMessageReceived,
)
from src.web import routes_ws
from src.web.server import WebServer

LATE_MARKER = "ответ-дописан-без-соединения"


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


@pytest.fixture(autouse=True)
def _clean_detached():
    """Реестр осиротевших генераций модульный — не таскаем его между тестами."""
    routes_ws._detached_generations.clear()
    yield
    routes_ws._detached_generations.clear()


def _make_server(bus: EventBus, sm: SessionManager, project_paths: list) -> WebServer:
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
        project_paths=project_paths,
    )


class _ProbeRelay:
    """Генерация, которая длится дольше, чем открыт WebSocket.

    Публикует agent_started, «думает» ``duration`` секунд, затем публикует
    финальный чанк. Отмену фиксирует флагом — по нему тест отличает
    «доработала» от «убили при разрыве».
    """

    def __init__(self, bus: EventBus, *, duration: float) -> None:
        self.bus = bus
        self.duration = duration
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self.completed = threading.Event()
        self._unsub = bus.subscribe(
            UserMessageReceived, self._handle, priority=SubscriberPriority.LOW
        )

    def close(self) -> None:
        self._unsub()

    async def _handle(self, event: UserMessageReceived) -> None:
        await self.bus.publish(
            AgentStarted(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
            )
        )
        self.started.set()
        try:
            await asyncio.sleep(self.duration)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        await self.bus.publish(
            AgentStreamingUpdate(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
                kind="text",
                content=LATE_MARKER,
            )
        )
        self.completed.set()


def _setup(tmp_dir, *, duration: float):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    project = tmp_dir / "proj"
    project.mkdir()
    session = sm.create_session(
        topic_id=-1, project_path=str(project), project_name="proj", chat_id=100
    )
    server = _make_server(bus, sm, [project])
    relay = _ProbeRelay(bus, duration=duration)
    return bus, sm, session, server, relay


def _login(client: TestClient) -> None:
    payload = _sign(
        "12345:abc", {"id": 100, "first_name": "A", "auth_date": int(time.time())}
    )
    assert client.post("/api/auth/telegram", json=payload).status_code == 200


def test_disconnect_does_not_kill_generation(tmp_dir):
    """Закрыли вкладку/ушли в другой чат — ответ дорабатывает."""
    bus, sm, session, server, relay = _setup(tmp_dir, duration=0.3)
    try:
        with TestClient(server.app) as client:
            _login(client)
            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                ws.send_json({"type": "user_message", "text": "привет"})
                assert relay.started.wait(timeout=5), "генерация не стартовала"
            # WS закрыт — здесь раньше стоял gen_task.cancel().
            assert relay.completed.wait(timeout=5), (
                "генерация не доработала после разрыва соединения"
            )
            assert not relay.cancelled.is_set(), "генерацию отменили при закрытии WS"
    finally:
        relay.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def test_stop_works_after_reconnect(tmp_dir):
    """«Стоп» останавливает генерацию, оставшуюся от прошлого соединения."""
    bus, sm, session, server, relay = _setup(tmp_dir, duration=60)
    finished = threading.Event()

    async def _on_finished(_event: AgentFinished) -> None:
        finished.set()

    unsub = bus.subscribe(AgentFinished, _on_finished)
    try:
        with TestClient(server.app) as client:
            _login(client)
            url = f"/api/ws/sessions/{session.session_uuid}"
            with client.websocket_connect(url) as ws:
                ws.send_json({"type": "user_message", "text": "привет"})
                assert relay.started.wait(timeout=5), "генерация не стартовала"

            # Вернулись в диалог: новое соединение, генерация всё ещё идёт.
            with client.websocket_connect(url) as ws2:
                ws2.send_json({"type": "stop_generation"})
                assert relay.cancelled.wait(timeout=5), (
                    "«Стоп» не остановил фоновую генерацию прошлого соединения"
                )
                # finished обязателен: по нему у клиента гаснет индикатор.
                assert finished.wait(timeout=5), "AgentFinished не опубликован"

            assert not relay.completed.is_set()
    finally:
        unsub()
        relay.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()
