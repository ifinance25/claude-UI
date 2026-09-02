from __future__ import annotations

import contextlib
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bot.handlers import callbacks
from src.bot.keyboards.project_select import create_settings_keyboard
from src.claude.session import SessionManager


@contextlib.contextmanager
def _windows_safe_tempdir():
    """Windows-safe replacement for tempfile.TemporaryDirectory.

    SQLite keeps connections open on .db files; on Windows the default
    cleanup raises PermissionError. mkdtemp + rmtree(ignore_errors)
    avoids that without changing assertion logic.
    """
    path = tempfile.mkdtemp()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class SubagentSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_keyboard_exposes_subagent_tracking_toggle(self) -> None:
        keyboard = create_settings_keyboard(
            show_token_usage=True,
            enable_subagent_tracking=False,
        )

        buttons = [
            (button.text, button.callback_data)
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertIn(
            ("Subagent logs: OFF", "settings:subagent_tracking:on"),
            buttons,
        )

    async def test_settings_callback_updates_topic_subagent_tracking(self) -> None:
        with _windows_safe_tempdir() as tmpdir:
            session_manager = SessionManager(storage_path=f"{tmpdir}/sessions.json")
            session_manager.create_session(
                topic_id=77,
                project_path=tmpdir,
                project_name="demo",
            )

            settings = SimpleNamespace(display=SimpleNamespace(show_token_usage=True))
            callbacks.router.settings = settings
            callbacks.router.session_manager = session_manager

            message = SimpleNamespace(
                message_thread_id=77,
                edit_reply_markup=AsyncMock(),
                delete=AsyncMock(),
            )
            callback = SimpleNamespace(
                data="settings:subagent_tracking:on",
                message=message,
                answer=AsyncMock(),
            )

            await callbacks.on_settings(callback)

            self.assertTrue(session_manager.get_session(77).enable_subagent_tracking)
            callback.answer.assert_awaited_once_with("Подробные логи сабагентов включены")


if __name__ == "__main__":
    unittest.main()
