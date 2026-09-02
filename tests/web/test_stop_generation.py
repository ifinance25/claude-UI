"""stop_generation over WS отменяет текущую генерацию, сессия переиспользуема.

Сценарий (Task 5, B-backend):
1. Открываем authed WS к сессии, шлём user_message.
2. Дожидаемся agent_started (генерация «в полёте»).
3. Шлём {"type": "stop_generation"} — текущая генерация отменяется.
4. Получаем finished (индикатор у клиента гаснет).
5. Сессия переиспользуема: новый user_message снова даёт agent_started.

Реальный ClaudeEventRelay тут не подключён. Вместо него — тестовый
подписчик на UserMessageReceived, который имитирует долгую генерацию:
публикует AgentStarted + streaming_update, затем «висит» в asyncio.sleep
до отмены. Это в точности то, что reader оборачивает в фоновую задачу
current_gen["task"] = create_task(bus.publish(UserMessageReceived(...))).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus, SubscriberPriority
from src.event_bus.events import (
    AgentStarted,
    AgentStreamingUpdate,
    UserMessageReceived,
)
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


class _SlowFakeRelay:
    """Имитирует in-flight генерацию Claude как подписчик EventBus.

    Публикует agent_started + один text-чанк, потом «висит» в долгом sleep,
    держа publish(UserMessageReceived) незавершённым — ровно тот случай,
    который reader должен уметь отменить по stop_generation. Считает старты,
    чтобы тест убедился, что после стопа сессия переиспользуема.
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.starts = 0
        # LOW: запускаемся ПОСЛЕ форвардера (HIGHEST) — как настоящий relay.
        self._unsub = bus.subscribe(
            UserMessageReceived,
            self._handle,
            priority=SubscriberPriority.LOW,
        )

    def close(self) -> None:
        self._unsub()

    async def _handle(self, event: UserMessageReceived) -> None:
        self.starts += 1
        await self.bus.publish(
            AgentStarted(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
            )
        )
        await self.bus.publish(
            AgentStreamingUpdate(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
                kind="text",
                content="thinking...",
            )
        )
        # Долгая «генерация» — висим до отмены извне (task.cancel()).
        await asyncio.sleep(60)


def test_stop_generation_cancels_and_session_reusable(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    # Сессия должна лежать внутри настроенного корня проектов, иначе
    # resolve_project_access (deny-by-default) отклонит user_message ещё
    # до запуска генерации.
    project = tmp_dir / "proj"
    project.mkdir()
    session = sm.create_session(
        topic_id=-1, project_path=str(project), project_name="proj", chat_id=100
    )
    server = _make_server(bus, sm, [project])
    relay = _SlowFakeRelay(bus)

    try:
        with TestClient(server.app) as client:
            login = _sign(
                "12345:abc",
                {"id": 100, "first_name": "A", "auth_date": int(time.time())},
            )
            resp = client.post("/api/auth/telegram", json=login)
            assert resp.status_code == 200

            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                # 1. Старт генерации.
                ws.send_json({"type": "user_message", "text": "hi"})

                # 2. Дожидаемся, что генерация «в полёте»: эхо user_message,
                #    agent_started, и хотя бы один text-чанк.
                frames = _drain_until(ws, "agent_started")
                assert any(f["type"] == "agent_started" for f in frames)

                # 3. Стоп.
                ws.send_json({"type": "stop_generation"})

                # 4. finished обязан прийти (индикатор гаснет).
                fin = _drain_until(ws, "finished")
                assert any(f["type"] == "finished" for f in fin)

                # 5. Сессия переиспользуема: новый user_message → agent_started.
                ws.send_json({"type": "user_message", "text": "again"})
                again = _drain_until(ws, "agent_started")
                assert any(f["type"] == "agent_started" for f in again)

        # Relay должен был стартовать дважды (до и после стопа).
        assert relay.starts == 2
    finally:
        relay.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def _drain_until(ws, target_type: str, *, timeout: float = 5.0) -> list[dict]:
    """Читает кадры, пока не встретит target_type (или таймаут).

    starlette TestClient WS блокирующий, поэтому таймаут эмулируем числом
    попыток: каждый receive_json ждёт следующий кадр; форвардер шлёт их
    почти мгновенно. На случай зависшего бэкенда ограничиваем число кадров.
    """
    frames: list[dict] = []
    for _ in range(200):
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") == target_type:
            return frames
    raise AssertionError(
        f"did not receive {target_type!r}; got {[f.get('type') for f in frames]}"
    )
