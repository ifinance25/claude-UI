"""Bus → WebSocket fan-out by session_uuid.

Each connected WS handler calls ``register(session_uuid=...)`` to get a
private ``asyncio.Queue`` that receives only events for that session. The
forwarder subscribes to ``UserMessageReceived``, ``AgentStarted``,
``AgentStreamingUpdate``, and ``AgentFinished`` on the shared ``EventBus``
and dispatches them. Subscribers must call ``unregister`` on disconnect.

session_uuid is used as the queue key (not topic_id) so the same channel
serves both web-originated sessions and Telegram-originated ones with a
single stable identifier across reconnects and channel migrations.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from src.event_bus.bus import EventBus, SubscriberPriority
from src.event_bus.events import (
    AgentFinished,
    AgentStarted,
    AgentStreamingUpdate,
    UserMessageReceived,
)

logger = structlog.get_logger()


class WSForwarder:
    """Maintains per-session queues that WebSocket handlers drain."""

    def __init__(self, *, bus: EventBus) -> None:
        self.bus = bus
        self._unsubs: list = []
        self._queues: dict[str, list[asyncio.Queue]] = {}
        # request_id → time.monotonic() старта, чтобы посчитать время
        # ответа Claude и отдать его клиенту в кадре finished.
        self._start_monotonic: dict[str, float] = {}

    async def start(self) -> None:
        # HIGHEST on UserMessageReceived ensures the web client sees the
        # user-message echo BEFORE ClaudeEventRelay (LOW) begins
        # publishing the agent's reply chain. Otherwise the WS would
        # receive agent_started/text/finished before the user_message
        # bubble that triggered them.
        self._unsubs = [
            self.bus.subscribe(
                UserMessageReceived,
                self._on_user_message,
                priority=SubscriberPriority.HIGHEST,
            ),
            self.bus.subscribe(AgentStarted, self._on_started),
            self.bus.subscribe(AgentStreamingUpdate, self._on_update),
            self.bus.subscribe(AgentFinished, self._on_finished),
        ]
        logger.info("ws_forwarder_started")

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        self._queues.clear()
        logger.info("ws_forwarder_stopped")

    def register(self, *, session_uuid: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._queues.setdefault(session_uuid, []).append(queue)
        return queue

    def unregister(self, *, session_uuid: str, queue: asyncio.Queue) -> None:
        lst = self._queues.get(session_uuid, [])
        if queue in lst:
            lst.remove(queue)
        if not lst and session_uuid in self._queues:
            del self._queues[session_uuid]

    def _fanout(self, session_uuid: str, message: dict[str, Any]) -> None:
        if not session_uuid:
            # An empty session_uuid means a publisher forgot to populate
            # the field. Without a warning this would silently swallow
            # streaming updates that web clients never see.
            logger.warning(
                "ws_fanout_missing_session_uuid",
                message_type=message.get("type"),
                hint="publisher must populate session_uuid; check the upstream emitter",
            )
            return
        for queue in list(self._queues.get(session_uuid, [])):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Очередь медленного клиента переполнена — событие
                # потеряно. Чтобы клиент не остался с молчаливой
                # потерей, посылаем gap-маркер: фронт по нему
                # принудительно переподключится и доберёт пропущенное
                # через REST /api/messages?since=<last_event_id>.
                logger.warning("ws_queue_full_dropped", session_uuid=session_uuid)
                try:
                    queue.put_nowait(
                        {
                            "type": "gap",
                            "session_uuid": session_uuid,
                            "reason": "client_too_slow",
                        }
                    )
                except asyncio.QueueFull:
                    # Очередь забита настолько, что даже gap-маркер не
                    # лезет — нечего делать, клиент скоро отвалится по
                    # таймауту heartbeat.
                    pass

    async def _on_user_message(self, event: UserMessageReceived) -> None:
        self._fanout(
            event.session_uuid,
            {
                "type": "user_message",
                "request_id": event.request_id,
                "content": event.text,
                "source": event.source,
            },
        )

    async def _on_started(self, event: AgentStarted) -> None:
        self._start_monotonic[event.request_id] = time.monotonic()
        self._fanout(
            event.session_uuid,
            {"type": "agent_started", "request_id": event.request_id},
        )

    async def _on_update(self, event: AgentStreamingUpdate) -> None:
        self._fanout(
            event.session_uuid,
            {
                "type": "streaming_update",
                "request_id": event.request_id,
                "kind": event.kind,
                "content": event.content,
                "metadata": event.metadata,
            },
        )

    async def _on_finished(self, event: AgentFinished) -> None:
        started = self._start_monotonic.pop(event.request_id, None)
        elapsed_ms = (
            int((time.monotonic() - started) * 1000) if started is not None else 0
        )
        self._fanout(
            event.session_uuid,
            {
                "type": "finished",
                "request_id": event.request_id,
                "session_id": event.session_id,
                "response_text": event.response_text,
                "usage": event.usage,
                "error": event.error,
                "elapsed_ms": elapsed_ms,
            },
        )
