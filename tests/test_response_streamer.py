from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.utils.streaming import ResponseStreamer, StreamingState


class ResponseStreamerFinalizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_skips_empty_success_message_when_disabled(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock())
        streamer = ResponseStreamer(bot=bot)
        state = StreamingState(chat_id=1, topic_id=2)
        state._done_event.set()
        state._receiving = False

        await streamer.finalize(
            state,
            show_token_usage=False,
            send_empty_completion=False,
        )

        bot.send_message.assert_not_awaited()

    async def test_finalize_deletes_log_by_default(self) -> None:
        """finalize() should delete the log message when keep_log=False."""
        mock_delete = AsyncMock()
        mock_edit = AsyncMock()
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            delete_message=mock_delete,
            edit_message_text=mock_edit,
        )
        streamer = ResponseStreamer(bot=bot)
        log_msg = SimpleNamespace(message_id=123, text="⏳ 10с · 2 tools", delete=AsyncMock())
        state = StreamingState(chat_id=1, topic_id=2, log_message=log_msg, log_buffer="⏳ 10с · 2 tools")
        state._done_event.set()
        state._receiving = False

        await streamer.finalize(state, keep_log=False)

        log_msg.delete.assert_awaited_once()
        mock_edit.assert_not_awaited()

    async def test_finalize_keeps_log_when_requested(self) -> None:
        """finalize() should update (not delete) log message when keep_log=True."""
        log_msg_edit = AsyncMock()
        bot = SimpleNamespace(send_message=AsyncMock())
        streamer = ResponseStreamer(bot=bot)
        log_msg = SimpleNamespace(
            message_id=123, text="⏳ 10с · 2 tools", edit_text=log_msg_edit,
        )
        state = StreamingState(chat_id=1, topic_id=2, log_message=log_msg, log_buffer="⏳ 10с · 2 tools")
        state._done_event.set()
        state._receiving = False

        await streamer.finalize(state, keep_log=True)

        log_msg_edit.assert_awaited_once()
        # Verify the text was changed from ⏳ to ✅
        call_args = log_msg_edit.call_args
        text_arg = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        self.assertIn("✅", text_arg)


class ExtractToolNameTests(unittest.TestCase):
    """Tests for _extract_tool_name helper."""

    def test_strips_bash_emoji(self) -> None:
        from src.bot.handlers.messages import _extract_tool_name
        self.assertEqual(_extract_tool_name("\U0001f527 Bash"), "Bash")

    def test_strips_read_emoji(self) -> None:
        from src.bot.handlers.messages import _extract_tool_name
        self.assertEqual(_extract_tool_name("\U0001f50d Read"), "Read")

    def test_strips_write_emoji(self) -> None:
        from src.bot.handlers.messages import _extract_tool_name
        self.assertEqual(_extract_tool_name("\U0001f4c4 Write"), "Write")

    def test_no_emoji_passthrough(self) -> None:
        from src.bot.handlers.messages import _extract_tool_name
        self.assertEqual(_extract_tool_name("Bash"), "Bash")


class FormatContextHeaderTests(unittest.TestCase):
    """Tests for _format_context_header helper."""

    def test_formats_tokens_as_k_with_percentage(self) -> None:
        from src.bot.handlers.messages import _format_context_header

        # total context = 50K + 30K + 25K = 105K, limit = 1M, pct = 10.5%
        cumulative = {
            "total_input_tokens": 50000,
            "total_cache_read_tokens": 30000,
            "total_cache_creation_tokens": 25000,
        }
        result = _format_context_header(cumulative)
        self.assertIn("105K", result)
        self.assertIn("1M", result)
        self.assertIn("10.5%", result)

    def test_returns_empty_for_none(self) -> None:
        from src.bot.handlers.messages import _format_context_header
        self.assertEqual(_format_context_header(None), "")

    def test_returns_empty_for_zero(self) -> None:
        from src.bot.handlers.messages import _format_context_header
        self.assertEqual(_format_context_header({}), "")


if __name__ == "__main__":
    unittest.main()
