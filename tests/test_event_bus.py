from __future__ import annotations

import unittest

from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.event_bus import ClaudeEventRelay, EventBus
from src.event_bus.events import AgentFinished, AgentStarted, AgentStreamingUpdate, UserMessageReceived


class _FakeBridge:
    def __init__(self, events: list[ClaudeEvent]) -> None:
        self.events = events
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        for event in self.events:
            yield event


class EventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribe_receives_events_until_unsubscribed(self) -> None:
        bus = EventBus()
        received: list[tuple[str, str]] = []

        unsubscribe = bus.subscribe(
            AgentStreamingUpdate,
            lambda event: received.append((event.kind, event.content)),
        )

        await bus.publish(
            AgentStreamingUpdate(
                request_id="req-1",
                chat_id=1,
                topic_id=2,
                kind="text",
                content="hello",
            )
        )
        unsubscribe()
        await bus.publish(
            AgentStreamingUpdate(
                request_id="req-1",
                chat_id=1,
                topic_id=2,
                kind="text",
                content="ignored",
            )
        )

        self.assertEqual(received, [("text", "hello")])

    async def test_claude_relay_translates_bridge_events_into_agent_events(self) -> None:
        bus = EventBus()
        bridge = _FakeBridge(
            [
                ClaudeEvent(ClaudeEventType.INIT, metadata={"session_id": "sdk-session"}),
                ClaudeEvent(ClaudeEventType.TOOL_USE, "🔧 Read\n   📂 src/app.py"),
                ClaudeEvent(ClaudeEventType.TEXT, "hello"),
                ClaudeEvent(
                    ClaudeEventType.COMPLETE,
                    metadata={
                        "session_id": "sdk-session",
                        "usage": {"input_tokens": 3, "output_tokens": 5},
                    },
                ),
            ]
        )
        ClaudeEventRelay(bus=bus, claude_bridge=bridge)

        seen: list[tuple[str, str | dict | None]] = []
        bus.subscribe(
            AgentStarted,
            lambda event: seen.append(("started", event.request_id)),
        )
        bus.subscribe(
            AgentStreamingUpdate,
            lambda event: seen.append((event.kind, event.content or event.metadata)),
        )
        bus.subscribe(
            AgentFinished,
            lambda event: seen.append(("finished", event.usage)),
        )

        await bus.publish(
            UserMessageReceived(
                request_id="req-1",
                chat_id=111,
                topic_id=77,
                project_path="/tmp/demo-project",
                project_name="demo-project",
                text="hello from telegram",
                session_id="existing-session",
            )
        )

        self.assertEqual(bridge.calls[0]["message"], "hello from telegram")
        self.assertEqual(bridge.calls[0]["topic_id"], 77)
        self.assertEqual(bridge.calls[0]["project_path"], "/tmp/demo-project")
        self.assertEqual(bridge.calls[0]["session_id"], "existing-session")
        self.assertEqual(bridge.calls[0]["attachments"], [])
        self.assertEqual(
            seen,
            [
                ("started", "req-1"),
                ("init", {"session_id": "sdk-session"}),
                ("tool_use", "🔧 Read\n   📂 src/app.py"),
                # text-чанк публикуется ЦЕЛИКОМ (раньше шёл по символам и
                # переполнял WS-очередь — на вебе ответ обрезался).
                ("text", "hello"),
                ("finished", {"input_tokens": 3, "output_tokens": 5}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
