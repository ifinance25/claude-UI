from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bot.handlers import messages
from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.claude.session import TopicSession
from src.event_bus import ClaudeEventRelay, EventBus
from src.event_bus.events import AgentStreamingUpdate, UserMessageReceived


class _FakeStreamer:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            tool_header_lines=[],
            response_buffer="",
            last_log_update=0,
        )
        self.streamed: list[str] = []
        self.finalize_calls: list[dict] = []

    async def create_log_message(self, *, chat_id: int, topic_id: int):
        return self.state

    async def update_log(self, state, content: str) -> None:
        return None

    async def stream_response(self, state, content: str) -> None:
        self.streamed.append(content)
        state.response_buffer += content

    async def finalize(self, state, *, show_token_usage: bool, usage=None, cumulative=None, send_empty_completion=True, show_context_usage: bool = False, keep_log: bool = False) -> None:
        self.finalize_calls.append(
            {
                "buffer": state.response_buffer,
                "show_token_usage": show_token_usage,
                "usage": usage,
                "cumulative": cumulative,
                "send_empty_completion": send_empty_completion,
            }
        )


class _FakeBridge:
    def __init__(self, events: list[ClaudeEvent]) -> None:
        self.events = events
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        for event in self.events:
            yield event


class _TrackingEventBus(EventBus):
    def __init__(self) -> None:
        super().__init__()
        self.subscribed_types: list[type] = []
        self.published_types: list[type] = []

    def subscribe(self, event_type, callback):
        self.subscribed_types.append(event_type)
        return super().subscribe(event_type, callback)

    async def publish(self, event):
        self.published_types.append(type(event))
        await super().publish(event)


class MessageEventBusIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_message_streams_via_event_bus_subscriptions(self) -> None:
        streamer = _FakeStreamer()
        with tempfile.TemporaryDirectory() as tmp_project:
            await self._run_event_bus_test(streamer, tmp_project)

    async def _run_event_bus_test(self, streamer: _FakeStreamer, project_path: str) -> None:
        session = TopicSession(
            topic_id=91,
            session_id="session-1",
            project_path=project_path,
            project_name="demo",
        )
        events = [
            ClaudeEvent(ClaudeEventType.TEXT, "Final answer"),
            ClaudeEvent(
                ClaudeEventType.COMPLETE,
                metadata={"usage": {"output_tokens": 1}, "session_id": "session-2"},
            ),
        ]
        bridge = _FakeBridge(events)
        event_bus = _TrackingEventBus()
        ClaudeEventRelay(bus=event_bus, claude_bridge=bridge)
        had_previous_event_bus = hasattr(messages.router, "event_bus")
        previous_event_bus = getattr(messages.router, "event_bus", None)

        messages.router.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False, show_context_usage=False, keep_log_after_response=False),
            get_light_project_paths=lambda: [],
        )
        messages.router.session_manager = SimpleNamespace(
            get_session=lambda topic_id: session,
            async_get_session=AsyncMock(return_value=session),
            mark_renamed=Mock(),
            async_mark_renamed=AsyncMock(),
            set_status=Mock(),
            async_set_status=AsyncMock(),
            update_session_id=Mock(),
            async_update_session_id=AsyncMock(),
            add_usage=Mock(),
            async_add_usage=AsyncMock(),
            get_usage=Mock(return_value={}),
            async_get_usage=AsyncMock(return_value={}),
            clear_session_id=Mock(),
            async_clear_session_id=AsyncMock(),
        )
        messages.router.claude_bridge = bridge
        messages.router.streamer = streamer
        messages.router.bot = SimpleNamespace(send_chat_action=AsyncMock())
        messages.router.event_bus = event_bus

        message = SimpleNamespace(
            text="Investigate this",
            chat=SimpleNamespace(id=555),
            message_thread_id=91,
            from_user=SimpleNamespace(username="tester", id=1),
            answer=AsyncMock(),
        )

        async def _noop(*args, **kwargs):
            return None

        try:
            with patch("src.bot.handlers.messages._typing_loop", new=_noop), patch(
                "src.bot.handlers.messages._heartbeat_loop",
                new=_noop,
            ):
                await messages.handle_message(message)
        finally:
            if had_previous_event_bus:
                messages.router.event_bus = previous_event_bus
            else:
                delattr(messages.router, "event_bus")

        self.assertEqual(bridge.calls[0]["message"], "Investigate this")
        self.assertIn(AgentStreamingUpdate, event_bus.subscribed_types)
        self.assertIn(UserMessageReceived, event_bus.published_types)
        self.assertEqual("".join(streamer.streamed), "Final answer")


if __name__ == "__main__":
    unittest.main()
