from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bot.handlers import callbacks, commands, messages


class OnboardingHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_uses_vels_claude_copy(self) -> None:
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1),
            from_user=SimpleNamespace(username="tester"),
            answer=AsyncMock(),
        )

        # cmd_start теперь принимает CommandObject (deeplink); без payload —
        # обычный онбординг.
        await commands.cmd_start(message, SimpleNamespace(args=None))

        text = message.answer.await_args.args[0]
        self.assertIn("Vels Claude", text)
        self.assertIn("Claude Code", text)
        self.assertIn("General chat", text)
        self.assertEqual(message.answer.await_args.kwargs["parse_mode"], "HTML")

    async def test_auth_uses_service_diagnostics(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(username="tester"),
            answer=AsyncMock(),
        )

        await commands.cmd_auth(message)

        text = message.answer.await_args.args[0]
        self.assertIn("production installer", text)
        self.assertIn("journalctl -u vels-claude", text)
        self.assertEqual(message.answer.await_args.kwargs["parse_mode"], "HTML")

    async def test_no_session_prompts_with_vels_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "demo"
            project.mkdir()
            settings = SimpleNamespace(
                get_light_project_paths=lambda: [project],
                get_projects_directory=lambda: Path(tmp),
                display=SimpleNamespace(show_logs=False),
            )
            messages.router.settings = settings  # type: ignore[attr-defined]
            # Light привязывает единственный проект сам: клавиатуры выбора нет,
            # человеку не нужно нажимать кнопку и присылать сообщение заново.
            create_session = AsyncMock()
            messages.router.session_manager = SimpleNamespace(
                async_get_session=AsyncMock(return_value=None),
                async_create_session=create_session,
            )
            messages.router.claude_bridge = SimpleNamespace()
            messages.router.streamer = SimpleNamespace()
            messages.router.bot = SimpleNamespace()
            messages.router.event_bus = None  # type: ignore[attr-defined]

            message = SimpleNamespace(
                text="hello",
                chat=SimpleNamespace(id=123),
                message_thread_id=91,
                from_user=SimpleNamespace(username="tester", id=7),
                answer=AsyncMock(),
            )

            await messages.process_incoming_text(message, "hello")

            create_session.assert_awaited_once()
            bound = create_session.await_args.kwargs
            self.assertEqual(bound["project_path"], str(project))
            self.assertEqual(bound["topic_id"], 91)
            # Клавиатура выбора не показывается — выбирать не из чего.
            for call in message.answer.await_args_list:
                self.assertNotIn("reply_markup", call.kwargs)


class OnboardingErrorStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_project_path_is_reported_before_claude_call(self) -> None:
        session = SimpleNamespace(
            project_name="missing",
            project_path="/tmp/vels-claude-definitely-missing",
            session_id=None,
            verbose_level=1,
            enable_subagent_tracking=False,
            is_renamed=True,
        )
        messages.router.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False, show_context_usage=False, keep_log_after_response=False),
            get_light_project_paths=lambda: [],
            get_projects_directory=lambda: Path("/tmp/projects"),
        )
        messages.router.session_manager = SimpleNamespace(
            async_get_session=AsyncMock(return_value=session),
            async_set_status=AsyncMock(),
        )
        bridge = SimpleNamespace(send_message=Mock())
        messages.router.claude_bridge = bridge
        messages.router.streamer = SimpleNamespace()
        messages.router.bot = SimpleNamespace()
        messages.router.event_bus = None  # type: ignore[attr-defined]

        message = SimpleNamespace(
            text="hello",
            chat=SimpleNamespace(id=123),
            message_thread_id=91,
            from_user=SimpleNamespace(username="tester", id=7),
            answer=AsyncMock(),
        )

        await messages.process_incoming_text(message, "hello")

        text = message.answer.await_args.args[0]
        # Windows-safe path comparison: backslashes -> forward slashes
        normalised = text.replace("\\", "/")
        self.assertIn("Папка проекта", text)
        self.assertIn(session.project_path.replace("\\", "/"), normalised)
        bridge.send_message.assert_not_called()

    async def test_auth_error_is_actionable(self) -> None:
        text = messages._format_error_for_user("auth", "Unauthorized")

        self.assertIn("Claude Code", text)
        self.assertIn("/auth", text)
        self.assertIn("service user", text)


if __name__ == "__main__":
    unittest.main()
