"""In-memory async event bus."""
from __future__ import annotations

import inspect
from collections import defaultdict
from enum import IntEnum
from typing import Awaitable, Callable, TypeVar

import structlog

from src.event_bus.events import BusEvent

logger = structlog.get_logger()

TEvent = TypeVar("TEvent", bound=BusEvent)
EventCallback = Callable[[TEvent], Awaitable[None] | None]


class SubscriberPriority(IntEnum):
    """Стандартные приоритеты подписчиков EventBus.

    Используем IntEnum, чтобы значения можно было передавать как обычные
    ``int`` в ``bus.subscribe(..., priority=SubscriberPriority.NORMAL)``,
    но без магических чисел, разбросанных по коду.

    Чем больше число — тем раньше выполняется подписчик. Подписчики
    одного приоритета — в порядке регистрации.
    """

    # Самые срочные — обычно UI/echo путь (WSForwarder), чтобы клиент
    # увидел эхо своего сообщения до того, как агент начнёт ответ.
    HIGHEST = 100
    # Персистентность / аудит — должны записать событие в БД ДО того,
    # как Claude-bridge запустит цепочку нестед-publish'ей (иначе при
    # быстром disconnect последнее сообщение терялось).
    NORMAL = 50
    # По умолчанию — Claude-bridge, FakeClaude, бизнес-логика.
    LOW = 0


class EventBus:
    """Simple in-memory publish/subscribe bus for async bot components.

    Subscribers are dispatched in *priority* order (higher first), then in
    registration order. Use a high priority for subscribers that must
    observe an event before any consumer that may publish further events
    in response — e.g. ``WSForwarder._on_user_message`` runs before
    ``ClaudeEventRelay._handle_user_message`` so the web client sees the
    user echo before the agent's reply starts streaming back.
    """

    def __init__(self) -> None:
        # value items are (priority, insertion_order, callback, critical) tuples
        self._subscribers: dict[
            type[BusEvent], list[tuple[int, int, EventCallback[BusEvent], bool]]
        ] = defaultdict(list)
        self._next_seq = 0

    def subscribe(
        self,
        event_type: type[TEvent],
        callback: EventCallback[TEvent],
        *,
        priority: int = 0,
        critical: bool = False,
    ) -> Callable[[], None]:
        """Register a callback and return an unsubscribe function.

        ``priority`` controls dispatch order: higher runs first. Ties are
        broken by registration order (FIFO). Default 0.

        ``critical`` marks a subscriber whose failure must NOT be silently
        swallowed: if it raises, ``publish`` logs and then re-raises, so
        downstream (lower-priority) subscribers don't run on a half-handled
        event. Use it for persistence that a later consumer depends on —
        e.g. the message persister must store ``user_message`` before the
        Claude bridge starts replying, otherwise the bubble would be lost
        from history while the agent still ran (CR3-5). Non-critical
        subscribers stay isolated (logged, skipped).
        """
        seq = self._next_seq
        self._next_seq += 1
        entry = (priority, seq, callback, critical)
        subscribers = self._subscribers[event_type]
        subscribers.append(entry)  # type: ignore[arg-type]
        # Re-sort: higher priority first, stable by insertion order.
        subscribers.sort(key=lambda e: (-e[0], e[1]))

        def unsubscribe() -> None:
            current = self._subscribers.get(event_type)
            if not current:
                return
            for i, existing in enumerate(current):
                if existing[2] is callback:
                    del current[i]
                    break
            if not current:
                self._subscribers.pop(event_type, None)

        return unsubscribe

    async def publish(self, event: BusEvent) -> None:
        """Publish an event to all matching subscribers in priority order.

        Non-critical subscriber failures are isolated: an exception is
        logged and the next subscriber still runs. Otherwise a flaky
        consumer (e.g. WSForwarder hitting a transient asyncio.QueueFull
        path) would silently prevent downstream subscribers like
        MessageHistoryPersister from observing the event.

        A subscriber registered with ``critical=True`` is the exception:
        its failure is logged and then re-raised, aborting the publish so
        lower-priority consumers don't act on a half-handled event (CR3-5).
        """
        callbacks: list[tuple[EventCallback[BusEvent], bool]] = []
        for event_type, subscribers in self._subscribers.items():
            if isinstance(event, event_type):
                callbacks.extend((entry[2], entry[3]) for entry in subscribers)

        for callback, critical in callbacks:
            try:
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.error(
                    "event_bus_subscriber_failed",
                    event_type=type(event).__name__,
                    callback=getattr(callback, "__qualname__", repr(callback)),
                    error=str(exc),
                    exc_class=type(exc).__name__,
                    critical=critical,
                    exc_info=True,
                )
                if critical:
                    # Don't let a later consumer (e.g. the Claude bridge)
                    # run on an event that a critical subscriber failed to
                    # process — surface the failure to the publisher.
                    raise
