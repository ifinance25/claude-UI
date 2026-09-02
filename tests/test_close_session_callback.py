from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.bot.handlers import callbacks


class CloseSessionCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_confirmation_closes_bridge_session_before_topic_cleanup(self) -> None:
        session_manager = SimpleNamespace(
            close_session=Mock(),
            async_close_session=AsyncMock(),
        )
        claude_bridge = SimpleNamespace(close_session=AsyncMock(return_value="session-1"))
        bot = SimpleNamespace(close_forum_topic=AsyncMock())

        callbacks.router.session_manager = session_manager
        callbacks.router.claude_bridge = claude_bridge
        callbacks.router.bot = bot

        callback = SimpleNamespace(
            message=SimpleNamespace(
                message_thread_id=91,
                chat=SimpleNamespace(id=555),
                edit_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

        await callbacks.on_close_confirm(callback)

        claude_bridge.close_session.assert_awaited_once_with(91)
        session_manager.async_close_session.assert_awaited_once_with(91)
        bot.close_forum_topic.assert_awaited_once_with(
            chat_id=555,
            message_thread_id=91,
        )
