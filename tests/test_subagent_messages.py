from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bot.handlers import messages
from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.claude.session import TopicSession


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

    async def finalize(
        self,
        state,
        *,
        show_token_usage: bool,
        usage=None,
        cumulative=None,
        send_empty_completion: bool | None = None,
        show_context_usage: bool = False,
        keep_log: bool = False,
    ) -> None:
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
        self._session_ids: dict[int, str] = {}

    async def send_message(self, **kwargs):
        for event in self.events:
            yield event


def _make_session_manager(session: TopicSession):
    """Build a mock session manager with all async methods."""
    sm = SimpleNamespace(
        # Async methods used by handle_message / process_incoming_text
        async_get_session=AsyncMock(return_value=session),
        async_mark_renamed=AsyncMock(),
        async_set_status=AsyncMock(),
        async_update_session_id=AsyncMock(),
        async_add_usage=AsyncMock(),
        async_get_usage=AsyncMock(return_value={}),
        async_clear_session_id=AsyncMock(),
        async_set_subagent_tracking=AsyncMock(),
        # Sync fallbacks (not normally called, but keep for safety)
        get_session=Mock(return_value=session),
        mark_renamed=Mock(),
        set_status=Mock(),
        update_session_id=Mock(),
        add_usage=Mock(),
        get_usage=Mock(return_value={}),
        clear_session_id=Mock(),
    )
    return sm


class SubagentMessageStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def _run_handler(self, *, enable_subagent_tracking: bool) -> _FakeStreamer:
        streamer = _FakeStreamer()
        with tempfile.TemporaryDirectory() as tmp_project:
            return await self._run_handler_with_path(streamer, enable_subagent_tracking, tmp_project)

    async def _run_handler_with_path(self, streamer: _FakeStreamer, enable_subagent_tracking: bool, project_path: str) -> _FakeStreamer:
        session = TopicSession(
            topic_id=91,
            session_id="session-1",
            project_path=project_path,
            project_name="demo",
        )
        session.enable_subagent_tracking = enable_subagent_tracking

        events = [
            ClaudeEvent(ClaudeEventType.SUBAGENT_START, "Researcher"),
            ClaudeEvent(ClaudeEventType.SUBAGENT_LOG, "> 🔧 Read\n>    📂 src/app.py"),
            ClaudeEvent(ClaudeEventType.SUBAGENT_FINISH, "Researcher"),
            ClaudeEvent(ClaudeEventType.TEXT, "Final answer"),
            ClaudeEvent(
                ClaudeEventType.COMPLETE,
                metadata={"usage": {"output_tokens": 1}, "session_id": "session-2"},
            ),
        ]

        messages.router.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False, show_context_usage=False, keep_log_after_response=False),
            get_light_project_paths=lambda: [],
        )
        messages.router.session_manager = _make_session_manager(session)
        messages.router.claude_bridge = _FakeBridge(events)
        messages.router.streamer = streamer
        messages.router.bot = SimpleNamespace(
            send_chat_action=AsyncMock(),
        )
        # Disable event bus path — test the direct bridge path
        messages.router.event_bus = None

        message = SimpleNamespace(
            text="Investigate this",
            chat=SimpleNamespace(id=555),
            message_thread_id=91,
            from_user=SimpleNamespace(username="tester", id=1),
            answer=AsyncMock(),
        )

        async def _noop(*args, **kwargs):
            return None

        with patch("src.bot.handlers.messages._typing_loop", new=_noop), patch(
            "src.bot.handlers.messages._heartbeat_loop",
            new=_noop,
        ):
            await messages.process_incoming_text(message, "Investigate this")

        return streamer

    async def test_subagent_tracking_off_streams_only_lifecycle_markers(self) -> None:
        streamer = await self._run_handler(enable_subagent_tracking=False)

        self.assertIn("🕵️‍♂️ Autonomous researcher launched...", streamer.streamed)
        self.assertIn("✅ Researcher finished its job.", streamer.streamed)
        self.assertNotIn("> 🔧 Read\n>    📂 src/app.py", streamer.streamed)

    async def test_subagent_tracking_on_streams_detailed_logs(self) -> None:
        streamer = await self._run_handler(enable_subagent_tracking=True)

        self.assertIn("🕵️‍♂️ Autonomous researcher launched...", streamer.streamed)
        self.assertIn("> 🔧 Read\n>    📂 src/app.py", streamer.streamed)
        self.assertIn("✅ Researcher finished its job.", streamer.streamed)


if __name__ == "__main__":
    unittest.main()
