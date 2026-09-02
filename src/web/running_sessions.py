"""Трекер сессий, по которым сейчас идёт генерация Claude.

Подписывается на AgentStarted (добавить) и AgentFinished (убрать) и
отдаёт срез генерирующих session_uuid. Используется листингом сессий,
чтобы фронт рисовал индикатор «думает» у активных диалогов.
"""
from __future__ import annotations

from src.event_bus.bus import EventBus
from src.event_bus.events import AgentFinished, AgentStarted


class RunningSessionsTracker:
    def __init__(self, bus: EventBus) -> None:
        self._running: set[str] = set()
        self._unsub_started = bus.subscribe(AgentStarted, self._on_started)
        self._unsub_finished = bus.subscribe(AgentFinished, self._on_finished)

    def _on_started(self, event: AgentStarted) -> None:
        if event.session_uuid:
            self._running.add(event.session_uuid)

    def _on_finished(self, event: AgentFinished) -> None:
        self._running.discard(event.session_uuid)

    def is_running(self, session_uuid: str) -> bool:
        return session_uuid in self._running

    def snapshot(self) -> set[str]:
        return set(self._running)

    def close(self) -> None:
        self._unsub_started()
        self._unsub_finished()
