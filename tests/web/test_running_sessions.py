"""RunningSessionsTracker: срез генерирующих сейчас сессий.

Подписан на AgentStarted (добавить session_uuid) и AgentFinished (убрать),
чтобы листинг сессий мог проставить is_running и фронт нарисовал «думает».
"""
import pytest

from src.event_bus.bus import EventBus
from src.event_bus.events import AgentFinished, AgentStarted
from src.web.running_sessions import RunningSessionsTracker


@pytest.mark.asyncio
async def test_tracks_running_sessions():
    bus = EventBus()
    tracker = RunningSessionsTracker(bus)
    assert tracker.is_running("s1") is False
    await bus.publish(AgentStarted(request_id="r1", chat_id=1, topic_id=1, session_uuid="s1"))
    assert tracker.is_running("s1") is True
    assert tracker.snapshot() == {"s1"}
    await bus.publish(AgentFinished(request_id="r1", chat_id=1, topic_id=1, session_uuid="s1"))
    assert tracker.is_running("s1") is False
