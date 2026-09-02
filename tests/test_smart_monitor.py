from __future__ import annotations

import asyncio
import os
import time
import unittest
from unittest.mock import patch

from src.claude.bridge import ClaudeBridge, ClaudeEventType

# os.openpty is Unix-only — skip the PTY-driven test on Windows.
_PTY_AVAILABLE = hasattr(os, "openpty")


class _FakePtyProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 4242
        self.killed = False
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class SmartMonitorTests(unittest.IsolatedAsyncioTestCase):
    def test_classifies_interactive_waiting_prompt(self) -> None:
        bridge = ClaudeBridge(idle_timeout_seconds=1)

        issue = bridge._detect_smart_monitor_issue("Need approval to continue [y/N]")

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.kind, "waiting_input")
        self.assertIn("[y/N]", issue.pattern)

    def test_classifies_terminal_crash_marker(self) -> None:
        bridge = ClaudeBridge(idle_timeout_seconds=1)

        issue = bridge._detect_smart_monitor_issue("npm ERR!\nKilled\n")

        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.kind, "process_error")
        self.assertEqual(issue.pattern, "Killed")

    @unittest.skipUnless(_PTY_AVAILABLE, "os.openpty is Unix-only")
    async def test_send_message_emits_error_when_interactive_prompt_stalls(self) -> None:
        bridge = ClaudeBridge(idle_timeout_seconds=0.05)
        process = _FakePtyProcess()
        reads = 0

        def fake_read(fd: int) -> bytes:
            nonlocal reads
            del fd
            reads += 1
            if reads == 1:
                return b"Press y to format [y/N]"
            time.sleep(0.2)
            return b""

        with patch(
            "src.claude.bridge.os.openpty",
            return_value=(10, 11),
        ), patch(
            "src.claude.bridge.os.close",
        ), patch(
            "src.claude.bridge.ClaudeBridge._read_pty_chunk",
            new=staticmethod(fake_read),
        ), patch(
            "src.claude.bridge.subprocess.Popen",
            return_value=process,
        ), patch(
            "src.claude.bridge._load_sdk",
            side_effect=RuntimeError("sdk unavailable"),
        ):
            events = [
                event
                async for event in bridge.send_message(
                    message="hello",
                    topic_id=7,
                    project_path=".",
                )
            ]

        self.assertEqual(events[0].type, ClaudeEventType.LOG)
        self.assertEqual(events[1].type, ClaudeEventType.ERROR)
        self.assertEqual(events[1].metadata["smart_monitor"], "waiting_input")
        self.assertTrue(process.killed)
        self.assertFalse(any(event.type == ClaudeEventType.COMPLETE for event in events))


if __name__ == "__main__":
    unittest.main()
