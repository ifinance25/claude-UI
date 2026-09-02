"""Разделитель «\\n\\n» после инструмента не режет слово пополам.

Регрессия из реального чата: ответ приходил как «М», а следующей строкой
«ы в demo-project» — markdown рисовал два абзаца, слово разрывалось.

Причина была в условии пост-тул разделителя: оно требовало, чтобы видимый
текст уже БЫЛ. Когда до инструмента шли только размышления, первая дельта
после инструмента («М») проскакивала мимо буфера и выставляла had_text_output,
поэтому разделитель прилипал ко ВТОРОЙ дельте — то есть внутрь слова.
"""
from __future__ import annotations

import unittest

from src.claude.bridge import (
    ClaudeBridge,
    ClaudeEventType,
    _SDKEventState,
)


def _delta(text: str, index: int = 1) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _text_start(index: int = 1) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        },
    }


def _tool_start(name: str = "Bash", index: int = 0) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "name": name, "id": "t1"},
        },
    }


def _block_stop(index: int) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": index},
    }


class PostToolSeparatorTests(unittest.TestCase):
    def _text(self, raw_events: list[dict]) -> str:
        """Склеенный видимый текст ответа — то, что увидит пользователь."""
        bridge = ClaudeBridge()
        state = _SDKEventState()
        out = []
        for raw in raw_events:
            out.extend(bridge._events_from_sdk_message(raw, state))
        return "".join(
            e.content for e in out if e.type == ClaudeEventType.TEXT and e.content
        )

    def test_word_not_split_when_tool_ran_before_any_text(self) -> None:
        """Инструмент отработал до первого видимого текста — разделителя нет."""
        text = self._text(
            [
                _tool_start(),
                _block_stop(0),
                _text_start(),
                _delta("М"),
                _delta("ы в demo-project"),
            ]
        )
        self.assertEqual(text, "Мы в demo-project")
        self.assertNotIn("\n\n", text)

    def test_separator_kept_when_text_preceded_the_tool(self) -> None:
        """Был текст, потом инструмент, потом снова текст — абзац разделяем."""
        text = self._text(
            [
                _text_start(),
                _delta("Смотрю файлы."),
                _block_stop(1),
                _tool_start(),
                _block_stop(0),
                _text_start(),
                _delta("Го"),
                _delta("тово"),
            ]
        )
        self.assertEqual(text, "Смотрю файлы.\n\nГотово")


if __name__ == "__main__":
    unittest.main()
