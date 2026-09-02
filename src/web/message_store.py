"""Persists bus events to the messages table for WS reconnect and history."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog

from src.claude.session import SessionManager
from src.event_bus.bus import EventBus, SubscriberPriority
from src.event_bus.events import (
    AgentFinished,
    AgentStarted,
    AgentStreamingUpdate,
    UserMessageReceived,
)

logger = structlog.get_logger()


class MessageHistoryPersister:
    """Subscribes to the bus and writes events to the messages table.

    Text-delta events are batched per topic_id and flushed as a single
    row, either when a non-text event arrives, when ``flush()`` is
    called, or when the periodic flusher loop ticks. This avoids one DB
    write per character while still preserving event order.

    Writes use ``topic_id`` because the ``messages`` table has a FK to
    ``sessions.topic_id``; ``session_uuid`` is the public identifier but
    not stored on ``messages`` directly (resolvable via JOIN if needed).
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        session_manager: SessionManager,
        text_flush_interval_ms: int = 200,
    ) -> None:
        self.bus = bus
        self.session_manager = session_manager
        self.text_flush_interval_ms = text_flush_interval_ms
        self._unsubs: list = []
        self._text_buffers: dict[int, dict[str, Any]] = {}
        self._flusher_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # request_id → monotonic-старт генерации, чтобы сохранить ВРЕМЯ ОТВЕТА
        # в метадату finished (иначе оно считается только в forwarder и теряется
        # при перезагрузке истории).
        self._start_monotonic: dict[str, float] = {}

    async def start(self) -> None:
        self._stop_event.clear()
        # NORMAL=50: ниже WSForwarder (HIGHEST=100, чтобы фронт получил
        # эхо сразу), но ВЫШЕ Claude-bridge/FakeClaude (LOW=0). Так
        # persister успевает записать user_message в БД ДО того, как
        # любой подписчик инициирует длинную цепочку нестед-publish'ей —
        # иначе при быстром закрытии WS-клиента (`finished` →
        # ws.close() → cancel reader) последний insert терялся.
        prio = SubscriberPriority.NORMAL
        self._unsubs = [
            # critical=True: a user_message MUST be persisted before the
            # Claude bridge (LOW prio) starts replying. If this insert
            # fails, abort the publish rather than running the agent on a
            # message that won't appear in history (CR3-5).
            self.bus.subscribe(
                UserMessageReceived, self._on_user_message, priority=prio, critical=True
            ),
            self.bus.subscribe(AgentStarted, self._on_agent_started, priority=prio),
            self.bus.subscribe(AgentStreamingUpdate, self._on_streaming_update, priority=prio),
            self.bus.subscribe(AgentFinished, self._on_agent_finished, priority=prio),
        ]
        self._flusher_task = asyncio.create_task(self._flush_loop())
        logger.info("message_persister_started")

    async def stop(self) -> None:
        self._stop_event.set()
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._flusher_task is not None:
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except asyncio.CancelledError:
                pass
            self._flusher_task = None
        await self.flush()
        logger.info("message_persister_stopped")

    async def flush(self) -> None:
        """Flush all pending text buffers to the DB."""
        for topic_id in list(self._text_buffers.keys()):
            await self._flush_topic(topic_id)

    async def _flush_topic(self, topic_id: int) -> None:
        buf = self._text_buffers.pop(topic_id, None)
        if buf is None or not buf["content"]:
            return
        await asyncio.to_thread(
            self._insert,
            topic_id=topic_id,
            request_id=buf["request_id"],
            type_="streaming_update",
            kind="text",
            content=buf["content"],
            metadata=None,
        )

    async def _flush_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.text_flush_interval_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    pass
                await self.flush()
        except asyncio.CancelledError:
            raise

    def _insert(
        self,
        *,
        topic_id: int,
        request_id: str | None,
        type_: str,
        kind: str | None,
        content: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        meta_json = json.dumps(metadata) if metadata else None
        conn = self.session_manager._get_connection()
        with self.session_manager._conn_lock:
            conn.execute(
                """
                INSERT INTO messages (topic_id, request_id, type, kind, content, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (topic_id, request_id, type_, kind, content, meta_json),
            )
            conn.commit()

    async def _on_user_message(self, event: UserMessageReceived) -> None:
        await self._flush_topic(event.topic_id)
        await asyncio.to_thread(
            self._insert,
            topic_id=event.topic_id,
            request_id=event.request_id,
            type_="user_message",
            kind=None,
            content=event.text,
            metadata={
                "chat_id": event.chat_id,
                "project_name": event.project_name,
                "source": event.source,
            },
        )

    async def _on_agent_started(self, event: AgentStarted) -> None:
        self._start_monotonic[event.request_id] = time.monotonic()
        await self._flush_topic(event.topic_id)
        await asyncio.to_thread(
            self._insert,
            topic_id=event.topic_id,
            request_id=event.request_id,
            type_="agent_started",
            kind=None,
            content="",
            metadata=None,
        )

    async def _on_streaming_update(self, event: AgentStreamingUpdate) -> None:
        if event.kind == "thinking":
            # Эфемерно: живые мысли не персистим (история берёт их из атомарного
            # блока в text). Ранний выход ДО флаша текст-буфера — чтобы живая
            # мысль не разрывала склейку текста того же хода.
            return
        if event.kind == "text":
            buf = self._text_buffers.get(event.topic_id)
            if buf is None or buf["request_id"] != event.request_id:
                await self._flush_topic(event.topic_id)
                self._text_buffers[event.topic_id] = {
                    "request_id": event.request_id,
                    "content": event.content,
                }
            else:
                buf["content"] += event.content
            return

        await self._flush_topic(event.topic_id)
        await asyncio.to_thread(
            self._insert,
            topic_id=event.topic_id,
            request_id=event.request_id,
            type_="streaming_update",
            kind=event.kind,
            content=event.content,
            metadata=event.metadata if event.metadata else None,
        )

    async def _on_agent_finished(self, event: AgentFinished) -> None:
        await self._flush_topic(event.topic_id)
        # Persist the Claude session_id onto the session row. The Telegram
        # handlers (bot/handlers/messages.py) do this themselves, but
        # web/event-bus sessions have no such handler — so without this
        # ``sessions.session_id`` stays NULL: web context is lost after a bot
        # restart, and the "Continue in Telegram" deeplink can't transfer the
        # session (it reads session_id from the DB). Guarded on a truthy
        # session_id so a None (error before init) never nulls a stored id.
        # Best-effort: a persistence failure must not break history writes.
        if event.session_id:
            try:
                await self.session_manager.async_update_session_id(
                    event.topic_id, event.session_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "session_id_persist_failed",
                    topic_id=event.topic_id,
                    error=str(exc),
                )
        started = self._start_monotonic.pop(event.request_id, None)
        metadata: dict[str, Any] = {
            "session_id": event.session_id,
            "usage": event.usage,
            "error": event.error,
        }
        # Время ответа — чтобы оно ВОССТАНАВЛИВАЛОСЬ при перезагрузке истории
        # (фронт патчит им usage-бабл хода). Совпадает с тем, что forwarder
        # шлёт в живом кадре finished.
        if started is not None:
            metadata["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        await asyncio.to_thread(
            self._insert,
            topic_id=event.topic_id,
            request_id=event.request_id,
            type_="finished",
            kind=None,
            content=event.response_text,
            metadata=metadata,
        )
