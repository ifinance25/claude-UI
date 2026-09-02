from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import src.claude.claude_settings as cs
from src.bot.handlers import messages


def _message(thread_id=91):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        message_thread_id=thread_id,
        answer=AsyncMock(),
    )


class ModelCommandTests(unittest.IsolatedAsyncioTestCase):
    def _wire(self, *, is_admin: int, current="claude-sonnet-5"):
        sm = SimpleNamespace(
            get_user_by_id=Mock(return_value={"is_admin": is_admin}),
            async_clear_session_id=AsyncMock(),
        )
        bridge = SimpleNamespace(clear_session_cache=Mock())
        messages.router.session_manager = sm
        messages.router.claude_bridge = bridge
        self.write = Mock()
        # _handle_native_command does a function-local `from
        # src.claude.claude_settings import read/write` at call time, so
        # patching the module attributes here is picked up. patch.object
        # restores them after each test (no leakage / no real-file writes).
        p_read = patch.object(
            cs, "read_claude_settings", Mock(return_value={"model": current})
        )
        p_write = patch.object(cs, "write_claude_settings", self.write)
        p_read.start()
        p_write.start()
        self.addCleanup(p_read.stop)
        self.addCleanup(p_write.stop)
        return sm, bridge

    async def test_bare_shows_keyboard_with_check(self):
        self._wire(is_admin=1, current="claude-opus-5")
        msg = _message()
        handled = await messages._handle_native_command("/model", "/model", msg)
        assert handled is True
        msg.answer.assert_awaited()
        markup = msg.answer.await_args.kwargs.get("reply_markup")
        assert markup is not None
        texts = [row[0].text for row in markup.inline_keyboard]
        assert any(t == "✓ Claude Opus 5" for t in texts)

    async def test_bare_non_admin_has_info_line(self):
        self._wire(is_admin=0)
        msg = _message()
        await messages._handle_native_command("/model", "/model", msg)
        body = msg.answer.await_args.args[0]
        assert "администратор" in body.lower()

    async def test_alias_admin_writes_pinned_id(self):
        sm, bridge = self._wire(is_admin=1, current="claude-sonnet-5")
        msg = _message()
        await messages._handle_native_command("/model", "/model opus", msg)
        self.write.assert_called_once()
        assert self.write.call_args.args[0]["model"] == "claude-opus-5"
        sm.async_clear_session_id.assert_awaited_once_with(91)
        bridge.clear_session_cache.assert_called_once_with(91)

    async def test_alias_non_admin_refused_no_write(self):
        self._wire(is_admin=0)
        msg = _message()
        await messages._handle_native_command("/model", "/model opus", msg)
        self.write.assert_not_called()
        body = msg.answer.await_args.args[0]
        assert "администратор" in body.lower()

    async def test_alias_unknown_token_errors_no_write(self):
        self._wire(is_admin=1)
        msg = _message()
        await messages._handle_native_command("/model", "/model gpt-4", msg)
        self.write.assert_not_called()
        body = msg.answer.await_args.args[0]
        assert "gpt-4" in body

    async def test_alias_html_special_token_is_escaped(self):
        self._wire(is_admin=1)
        msg = _message()
        await messages._handle_native_command("/model", "/model <b>", msg)
        self.write.assert_not_called()
        body = msg.answer.await_args.args[0]
        assert "<b>" not in body
        assert "&lt;b&gt;" in body
