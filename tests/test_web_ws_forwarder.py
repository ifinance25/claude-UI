"""Tests for WSForwarder: bus → per-session queue fan-out (keyed by session_uuid)."""
from __future__ import annotations

import asyncio

import pytest

from src.event_bus.bus import EventBus
from src.event_bus.events import AgentFinished, AgentStreamingUpdate
from src.web.ws_forwarder import WSForwarder


async def test_subscriber_receives_session_events():
    bus = EventBus()
    fwd = WSForwarder(bus=bus)
    await fwd.start()

    queue = fwd.register(session_uuid="sess-a")
    try:
        await bus.publish(
            AgentStreamingUpdate(
                request_id="r",
                chat_id=1,
                topic_id=42,
                session_uuid="sess-a",
                kind="text",
                content="hi",
            )
        )
        msg = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert msg["type"] == "streaming_update"
        assert msg["kind"] == "text"
        assert msg["content"] == "hi"
    finally:
        fwd.unregister(session_uuid="sess-a", queue=queue)
        await fwd.stop()


async def test_subscriber_ignores_other_sessions():
    bus = EventBus()
    fwd = WSForwarder(bus=bus)
    await fwd.start()

    queue = fwd.register(session_uuid="sess-a")
    try:
        await bus.publish(
            AgentStreamingUpdate(
                request_id="r",
                chat_id=1,
                topic_id=999,
                session_uuid="sess-other",
                kind="text",
                content="not for us",
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.1)
    finally:
        fwd.unregister(session_uuid="sess-a", queue=queue)
        await fwd.stop()


async def test_multiple_subscribers_for_same_session():
    bus = EventBus()
    fwd = WSForwarder(bus=bus)
    await fwd.start()

    q1 = fwd.register(session_uuid="sess-a")
    q2 = fwd.register(session_uuid="sess-a")
    try:
        await bus.publish(
            AgentFinished(
                request_id="r",
                chat_id=1,
                topic_id=42,
                session_uuid="sess-a",
                response_text="ok",
            )
        )
        m1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        m2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        assert m1["type"] == "finished"
        assert m2["type"] == "finished"
    finally:
        fwd.unregister(session_uuid="sess-a", queue=q1)
        fwd.unregister(session_uuid="sess-a", queue=q2)
        await fwd.stop()


async def test_events_without_session_uuid_are_dropped():
    """Defensive: events with empty session_uuid have no queue to fan into."""
    bus = EventBus()
    fwd = WSForwarder(bus=bus)
    await fwd.start()

    queue = fwd.register(session_uuid="sess-a")
    try:
        await bus.publish(
            AgentStreamingUpdate(
                request_id="r",
                chat_id=1,
                topic_id=1,
                # session_uuid omitted -> defaults to ""
                kind="text",
                content="orphan",
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.1)
    finally:
        fwd.unregister(session_uuid="sess-a", queue=queue)
        await fwd.stop()
