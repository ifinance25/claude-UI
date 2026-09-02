"""Tests for the `priority` parameter on EventBus.subscribe."""
from __future__ import annotations

from src.event_bus.bus import EventBus
from src.event_bus.events import UserMessageReceived


def _make_event() -> UserMessageReceived:
    return UserMessageReceived(
        request_id="r",
        chat_id=1,
        topic_id=1,
        project_path="/tmp",
        text="hi",
    )


async def test_higher_priority_runs_first():
    bus = EventBus()
    order: list[str] = []

    async def low(_event):
        order.append("low")

    async def high(_event):
        order.append("high")

    # Subscribe LOW first, then HIGH with priority — HIGH must still run first
    bus.subscribe(UserMessageReceived, low, priority=0)
    bus.subscribe(UserMessageReceived, high, priority=100)

    await bus.publish(_make_event())

    assert order == ["high", "low"]


async def test_equal_priority_preserves_registration_order():
    bus = EventBus()
    order: list[str] = []

    async def first(_event):
        order.append("first")

    async def second(_event):
        order.append("second")

    bus.subscribe(UserMessageReceived, first)
    bus.subscribe(UserMessageReceived, second)

    await bus.publish(_make_event())

    assert order == ["first", "second"]


async def test_unsubscribe_with_priority_works():
    bus = EventBus()
    order: list[str] = []

    async def high(_event):
        order.append("high")

    async def low(_event):
        order.append("low")

    unsub_high = bus.subscribe(UserMessageReceived, high, priority=100)
    bus.subscribe(UserMessageReceived, low)

    await bus.publish(_make_event())
    assert order == ["high", "low"]

    order.clear()
    unsub_high()
    await bus.publish(_make_event())
    assert order == ["low"]
