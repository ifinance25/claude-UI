from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from src.claude.bridge import ClaudeBridge, ClaudeEventType


class _FakeOptions:
    created: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        type(self).created.append(kwargs)


class ClaudeBridgeSDKTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _FakeOptions.created.clear()

    async def test_send_message_streams_sdk_events_without_subprocess(self) -> None:
        async def fake_query(*, prompt: str, options: _FakeOptions):
            self.assertEqual(prompt, "hello from telegram")
            # Windows-safe: bridge normalises the cwd via str(Path(...))
            # which yields '\tmp\demo-project' on win32. Compare as posix.
            self.assertEqual(
                str(options.kwargs["cwd"]).replace("\\", "/"),
                "/tmp/demo-project",
            )
            self.assertEqual(options.kwargs["resume"], "existing-session")
            yield {"type": "system", "subtype": "init", "session_id": "sdk-session"}
            yield {
                "type": "tool_use",
                "name": "Read",
                "input": {"file_path": "/tmp/demo-project/src/app.py"},
            }
            yield {"type": "text_delta", "text": "hello"}
            yield {
                "type": "result",
                "session_id": "sdk-session",
                "usage": {"input_tokens": 3, "output_tokens": 5},
                "total_cost_usd": 0.125,
            }

        bridge = ClaudeBridge()

        with patch(
            "src.claude.bridge._load_sdk",
            return_value=(_FakeOptions, fake_query),
            create=True,
        ), patch(
            "src.claude.bridge.asyncio.create_subprocess_exec",
            side_effect=AssertionError("subprocess launch must not be used"),
        ):
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
                ClaudeEventType.TOOL_USE,
                ClaudeEventType.TEXT,
                ClaudeEventType.USAGE,
                ClaudeEventType.COMPLETE,
            ],
        )
        self.assertEqual(events[0].metadata["session_id"], "sdk-session")
        self.assertEqual(events[2].content, "hello")
        self.assertIn("src/app.py", events[1].content)
        self.assertEqual(events[-1].metadata["session_id"], "sdk-session")
        self.assertEqual(events[-1].metadata["usage"]["input_tokens"], 3)
        self.assertEqual(events[-1].metadata["usage"]["output_tokens"], 5)
        self.assertEqual(events[-1].metadata["usage"]["cost_usd"], 0.125)
        self.assertEqual(_FakeOptions.created[0]["permission_mode"], "bypassPermissions")
        self.assertEqual(_FakeOptions.created[0]["setting_sources"], ["project", "user"])
        self.assertNotIn(77, bridge._active_processes)

    async def test_cancel_message_stops_active_sdk_run(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def fake_query(*, prompt: str, options: _FakeOptions):
            del prompt, options
            started.set()
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        bridge = ClaudeBridge()

        async def consume() -> list:
            return [
                event
                async for event in bridge.send_message(
                    message="stop me",
                    topic_id=91,
                    project_path="/tmp/demo-project",
                )
            ]

        with patch(
            "src.claude.bridge._load_sdk",
            return_value=(_FakeOptions, fake_query),
            create=True,
        ), patch(
            "src.claude.bridge.asyncio.create_subprocess_exec",
            side_effect=AssertionError("subprocess launch must not be used"),
        ):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertIn(91, bridge._active_processes)

            was_cancelled = await bridge.cancel_message(91)
            events = await asyncio.wait_for(task, timeout=1)

        self.assertTrue(was_cancelled)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(events[-1].type, ClaudeEventType.COMPLETE)
        self.assertNotIn(91, bridge._active_processes)

    async def test_closing_generator_aclose_sdk_iterator_deterministically(self) -> None:
        """При закрытии send_message-генератора (web stop_generation) SDK
        async-iterator закрывается через aclose() СИНХРОННО на пути отмены —
        так дочерний `claude` гасится детерминированно, а не по GC/atexit.
        """
        first_yielded = asyncio.Event()
        aclosed = asyncio.Event()

        class _FakeStream:
            """Имитирует SDK query(): отдаёт init, потом висит; помнит aclose."""

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not first_yielded.is_set():
                    first_yielded.set()
                    return {
                        "type": "system",
                        "subtype": "init",
                        "session_id": "sdk-session",
                    }
                # Дальше «генерация» висит до отмены.
                await asyncio.sleep(60)
                raise StopAsyncIteration

            async def aclose(self):
                aclosed.set()

        def fake_query(*, prompt: str, options):
            del prompt, options
            return _FakeStream()

        bridge = ClaudeBridge()

        with patch(
            "src.claude.bridge._load_sdk",
            return_value=(_FakeOptions, fake_query),
            create=True,
        ), patch(
            "src.claude.bridge.asyncio.create_subprocess_exec",
            side_effect=AssertionError("subprocess launch must not be used"),
        ):
            agen = bridge.send_message(
                message="stop me",
                topic_id=55,
                project_path="/tmp/demo-project",
            )
            first = await asyncio.wait_for(agen.__anext__(), timeout=1)
            self.assertEqual(first.type, ClaudeEventType.INIT)
            await asyncio.wait_for(first_yielded.wait(), timeout=1)

            # Закрываем генератор как это делает отменённый relay async-for.
            await asyncio.wait_for(agen.aclose(), timeout=2)

        # aclose() SDK-итератора обязан быть вызван детерминированно тут,
        # а не отложен до сборки мусора.
        self.assertTrue(aclosed.is_set())
        self.assertNotIn(55, bridge._active_processes)


if __name__ == "__main__":
    unittest.main()
