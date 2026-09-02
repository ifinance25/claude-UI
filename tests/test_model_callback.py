from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bot.handlers import callbacks


def _callback(data: str, *, thread_id=91):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=100),
        message=SimpleNamespace(
            message_thread_id=thread_id,
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


class ModelCallbackTests(unittest.IsolatedAsyncioTestCase):
    def _wire(self, *, is_admin: int, current="claude-sonnet-5"):
        sm = SimpleNamespace(
            get_user_by_id=Mock(return_value={"is_admin": is_admin}),
            async_clear_session_id=AsyncMock(),
        )
        bridge = SimpleNamespace(clear_session_cache=Mock())
        callbacks.router.session_manager = sm
        callbacks.router.claude_bridge = bridge
        callbacks.router.bot = SimpleNamespace()
        self.write = Mock()
        # patch.object restores the real functions after each test so the
        # mocks don't leak into other test files (and clobber real settings).
        p_read = patch.object(
            callbacks, "read_claude_settings", Mock(return_value={"model": current})
        )
        p_write = patch.object(callbacks, "write_claude_settings", self.write)
        p_read.start()
        p_write.start()
        self.addCleanup(p_read.stop)
        self.addCleanup(p_write.stop)
        return sm, bridge

    async def test_non_admin_gets_alert_and_no_write(self):
        sm, bridge = self._wire(is_admin=0)
        cb = _callback("model:set:claude-opus-5")
        await callbacks.on_model_action(cb)
        cb.answer.assert_awaited()
        assert any(
            call.kwargs.get("show_alert") for call in cb.answer.await_args_list
        )
        self.write.assert_not_called()
        sm.async_clear_session_id.assert_not_awaited()

    async def test_admin_switch_writes_and_clears_session(self):
        sm, bridge = self._wire(is_admin=1, current="claude-sonnet-5")
        cb = _callback("model:set:claude-opus-5")
        await callbacks.on_model_action(cb)
        self.write.assert_called_once()
        written = self.write.call_args.args[0]
        assert written["model"] == "claude-opus-5"
        sm.async_clear_session_id.assert_awaited_once_with(91)
        bridge.clear_session_cache.assert_called_once_with(91)
        cb.message.edit_text.assert_awaited()

    async def test_admin_same_model_no_write(self):
        sm, bridge = self._wire(is_admin=1, current="claude-opus-5")
        cb = _callback("model:set:claude-opus-5")
        await callbacks.on_model_action(cb)
        self.write.assert_not_called()
        sm.async_clear_session_id.assert_not_awaited()

    async def test_admin_unknown_model_rejected(self):
        sm, bridge = self._wire(is_admin=1)
        cb = _callback("model:set:gpt-4")
        await callbacks.on_model_action(cb)
        self.write.assert_not_called()

    async def test_no_thread_id_skips_session_clear_but_writes(self):
        sm, bridge = self._wire(is_admin=1, current="claude-sonnet-5")
        cb = _callback("model:set:claude-opus-5", thread_id=None)
        await callbacks.on_model_action(cb)
        self.write.assert_called_once()
        sm.async_clear_session_id.assert_not_awaited()
        bridge.clear_session_cache.assert_not_called()

    async def test_action_not_set_is_noop(self):
        self._wire(is_admin=1)
        cb = _callback("model:noop")
        await callbacks.on_model_action(cb)
        cb.answer.assert_awaited()
        self.write.assert_not_called()

    async def test_edit_message_not_modified_is_swallowed(self):
        self._wire(is_admin=1, current="claude-sonnet-5")
        cb = _callback("model:set:claude-opus-5")
        cb.message.edit_text = AsyncMock(
            side_effect=Exception("Bad Request: message is not modified")
        )
        await callbacks.on_model_action(cb)  # must not raise
        self.write.assert_called_once()
