from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.claude.bridge import ClaudeBridge, ClaudeEventType


def _result(*, stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class ClaudeBridgeTmuxTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_streams_events_from_tmux_capture(self) -> None:
        bridge = ClaudeBridge(
            transport="tmux",
            capture_poll_interval_seconds=0,
        )

        session_name = "claude_topic_77"
        init_line = json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "session_id": "tmux-session",
            }
        )
        text_line = json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "delta": {
                        "type": "text_delta",
                        "text": "hello from tmux",
                    }
                },
            }
        )
        result_line = json.dumps(
            {
                "type": "result",
                "session_id": "tmux-session",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 5,
                },
                "total_cost_usd": 0.125,
            }
        )

        captures = [
            "",
            init_line,
            "\n".join([init_line, text_line]),
            "\n".join([init_line, text_line, result_line]),
        ]
        calls: list[tuple[tuple[str, ...], bool]] = []

        async def fake_run_tmux(*args: str, check: bool = True) -> SimpleNamespace:
            calls.append((args, check))
            if args[:1] == ("has-session",):
                return _result(returncode=1)
            if args[:1] == ("capture-pane",):
                return _result(stdout=captures.pop(0))
            return _result()

        with patch.object(bridge, "_run_tmux", side_effect=fake_run_tmux):
            events = [
                event
                async for event in bridge.send_message(
                    message="hello from telegram",
                    topic_id=77,
                    project_path="/tmp/demo-project",
                    session_id="existing-session",
                )
            ]

        self.assertEqual(
            [event.type for event in events],
            [
                ClaudeEventType.INIT,
                ClaudeEventType.TEXT,
                ClaudeEventType.USAGE,
                ClaudeEventType.COMPLETE,
            ],
        )
        self.assertEqual(events[1].content, "hello from tmux")
        self.assertEqual(events[-1].metadata["session_id"], "tmux-session")
        self.assertEqual(events[-1].metadata["usage"]["input_tokens"], 3)
        self.assertEqual(events[-1].metadata["usage"]["output_tokens"], 5)
        self.assertEqual(events[-1].metadata["usage"]["cost_usd"], 0.125)
        self.assertEqual(bridge._session_ids[77], "tmux-session")
        self.assertNotIn(77, bridge._active_processes)

        send_literal_calls = [
            args
            for args, _ in calls
            if args[:4] == ("send-keys", "-t", session_name, "-l")
        ]
        self.assertEqual(len(send_literal_calls), 1)
        command = send_literal_calls[0][4]
        self.assertIn("claude -p", command)
        self.assertIn("--output-format stream-json", command)
        self.assertIn("--resume existing-session", command)
        self.assertIn("--dangerously-skip-permissions", command)
        # Windows-safe: normalise the -c path argument so backslashes
        # don't fool the membership check.
        normalised_calls = [
            tuple(str(a).replace("\\", "/") for a in args)
            for args, _ in calls
        ]
        self.assertIn(
            ("new-session", "-d", "-s", session_name, "-c", "/tmp/demo-project"),
            normalised_calls,
        )

    async def test_cancel_message_kills_tmux_session(self) -> None:
        bridge = ClaudeBridge(transport="tmux")
        bridge._active_processes[91] = SimpleNamespace(
            session_name="claude_topic_91",
            returncode=None,
            pid="tmux:claude_topic_91",
            task=None,
        )

        with patch.object(
            bridge,
            "_run_tmux",
            AsyncMock(return_value=_result()),
        ) as run_tmux:
            cancelled = await bridge.cancel_message(91)

        self.assertTrue(cancelled)
        run_tmux.assert_awaited_once_with(
            "kill-session",
            "-t",
            "claude_topic_91",
            check=False,
        )
        self.assertNotIn(91, bridge._active_processes)

    async def test_start_cleanup_loop_discovers_existing_tmux_sessions(self) -> None:
        bridge = ClaudeBridge(transport="tmux")

        with patch.object(
            bridge,
            "_run_tmux",
            AsyncMock(return_value=_result(stdout="claude_topic_12\nother-session\nclaude_topic_34\n")),
        ):
            await bridge.start_cleanup_loop()

        self.assertEqual(bridge._tmux_sessions[12], "claude_topic_12")
        self.assertEqual(bridge._tmux_sessions[34], "claude_topic_34")
        self.assertNotIn(999, bridge._tmux_sessions)
