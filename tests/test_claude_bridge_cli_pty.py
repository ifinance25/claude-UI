from __future__ import annotations

import errno
import os
import unittest
from unittest.mock import patch

from src.claude.bridge import ClaudeBridge, ClaudeEventType

# PTY (os.openpty) is Unix-only — the CLI-fallback path that patches it
# can't be exercised on Windows, where os.openpty doesn't exist.
_PTY_AVAILABLE = hasattr(os, "openpty")


class _FakePtyProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode: int | None = None
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


@unittest.skipUnless(_PTY_AVAILABLE, "os.openpty is Unix-only")
class ClaudeBridgeCliPtyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_fallback_uses_pty_and_cleans_terminal_chunks(self) -> None:
        bridge = ClaudeBridge(idle_timeout_seconds=0.05)
        process = _FakePtyProcess()
        chunks = iter(
            [
                b'\x1b[31m{"type":"system","subtype":"init","session_id":"pty-session"}\x1b[0m\n',
                b"\x1b[2Kprogress 1%\rprogress 2%\n",
                b'\x1b[31m{"type":"text_delta","text":"hel',
                b'lo"}\x1b[0m\n',
            ]
        )

        def fake_read(fd: int) -> bytes:
            del fd
            try:
                return next(chunks)
            except StopIteration as exc:
                raise OSError(errno.EIO, "pty closed") from exc

        with patch(
            "src.claude.bridge._load_sdk",
            side_effect=RuntimeError("sdk missing"),
        ), patch(
            "src.claude.bridge.asyncio.create_subprocess_exec",
            side_effect=AssertionError("PIPE subprocess must not be used"),
        ), patch(
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
        ):
            events = [
                event
                async for event in bridge.send_message(
                    message="hello from telegram",
                    topic_id=77,
                    project_path="/tmp/demo-project",
                )
            ]

        self.assertEqual(
            [event.type for event in events],
            [
                ClaudeEventType.INIT,
                ClaudeEventType.LOG,
                ClaudeEventType.TEXT,
                ClaudeEventType.COMPLETE,
            ],
        )
        self.assertEqual(events[0].metadata["session_id"], "pty-session")
        self.assertEqual(events[1].content, "progress 2%")
        self.assertEqual(events[2].content, "hello")
        self.assertEqual(events[-1].content, "hello")
        self.assertEqual(events[-1].metadata["session_id"], "pty-session")


if __name__ == "__main__":
    unittest.main()
