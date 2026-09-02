"""Partial-message streaming: dedup of the assembled AssistantMessage and
single-block coalescing of thinking deltas.

С include_partial_messages SDK отдаёт И посимвольные `stream_event`, И финальный
собранный `assistant` с тем же контентом. Бридж обязан отбросить собранный
дубль (по saw_stream_delta) и обернуть мышление в ОДИН THINKING-блок, а не в
россыпь маркеров вокруг каждой дельты.
"""
from __future__ import annotations

import unittest

from src.claude.bridge import (
    THINKING_CLOSE_MARKER,
    THINKING_OPEN_MARKER,
    ClaudeBridge,
    ClaudeEventType,
    _SDKEventState,
)


def _delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _thinking_start() -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
    }


def _thinking_delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


def _text_start() -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
    }


def _block_stop(index: int) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": index},
    }


def _tool_start(name: str, index: int = 0) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "name": name, "id": "t1"},
        },
    }


def _tool_input(partial_json: str, index: int = 0) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        },
    }


def _assistant(text: str) -> dict:
    # Реальный SDK отдаёт AssistantMessage с объектами TextBlock; _type_name
    # резолвит их в "TextBlock". Эмулируем это (dict с type="TextBlock"), а не
    # JSON-форму "text" из stream-json CLI (её _events_from_content_blocks не
    # разбирает — там текст приходит из result-фолбэка).
    return {
        "type": "assistant",
        "message": {"content": [{"type": "TextBlock", "text": text}], "model": "x"},
    }


class PartialStreamDedupTests(unittest.TestCase):
    def _run(self, raw_events: list[dict]) -> list:
        bridge = ClaudeBridge()
        state = _SDKEventState()
        out = []
        for raw in raw_events:
            out.extend(bridge._events_from_sdk_message(raw, state))
        return out

    def test_assembled_assistant_message_is_dropped_after_deltas(self) -> None:
        events = self._run(
            [
                _text_start(),
                _delta("Hel"),
                _delta("lo"),
                _block_stop(1),
                _assistant("Hello"),  # дубль — должен отброситься
            ]
        )
        texts = [e.content for e in events if e.type == ClaudeEventType.TEXT and e.content]
        self.assertEqual(texts, ["Hel", "lo"])
        self.assertEqual("".join(texts), "Hello")  # ровно один раз, без дубля

    def test_assembled_assistant_message_kept_without_partial(self) -> None:
        # check_auth / CLI-фолбэк: дельт не было → собранное сообщение нужно.
        events = self._run([_assistant("Full answer")])
        texts = [e.content for e in events if e.type == ClaudeEventType.TEXT]
        self.assertEqual(texts, ["Full answer"])

    def test_assistant_json_text_form_yields_events(self) -> None:
        # CLI/tmux stream-json отдаёт блоки как {"type":"text"} (а не TextBlock).
        # Раньше они терялись → пустой ответ; теперь обрабатываются обе формы.
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "first "},
                    {"type": "text", "text": "second"},
                ],
                "model": "x",
            },
        }
        events = self._run([msg])
        texts = [e.content for e in events if e.type == ClaudeEventType.TEXT]
        self.assertEqual(texts, ["first ", "second"])  # multi-block не режется

    def test_thinking_deltas_coalesce_into_single_block(self) -> None:
        events = self._run(
            [
                _thinking_start(),
                _thinking_delta("I'll "),
                _thinking_delta("think"),
                _block_stop(0),
                _text_start(),
                _delta("Answer"),
                _block_stop(1),
                _assistant("Answer"),  # дубль текста — отбрасывается
            ]
        )
        merged = "".join(
            e.content for e in events if e.type == ClaudeEventType.TEXT
        )
        # Ровно один открывающий и один закрывающий маркер мышления.
        self.assertEqual(merged.count(THINKING_OPEN_MARKER), 1)
        self.assertEqual(merged.count(THINKING_CLOSE_MARKER), 1)
        self.assertIn(f"{THINKING_OPEN_MARKER}I'll think{THINKING_CLOSE_MARKER}", merged)
        self.assertTrue(merged.endswith("Answer"))

    def test_streamed_write_tool_use_single_card_with_file_path(self) -> None:
        # Стримленный Write: ОДНА карточка tool_use (на content_block_stop) с
        # file_path в метадате — иначе веб не показал бы артефакт.
        events = self._run(
            [
                _tool_start("Write"),
                _tool_input('{"file_path":"out.md","content":"hi"}'),
                _block_stop(0),
            ]
        )
        tool_events = [e for e in events if e.type == ClaudeEventType.TOOL_USE]
        self.assertEqual(len(tool_events), 1)  # ровно одна карточка (без дубля)
        self.assertEqual(tool_events[0].metadata.get("name"), "Write")
        self.assertEqual(tool_events[0].metadata.get("file_path"), "out.md")

    def test_streamed_bash_tool_use_records_outputs(self) -> None:
        events = self._run(
            [
                _tool_start("Bash"),
                _tool_input('{"command":"cp -r src out-copy"}'),
                _block_stop(0),
            ]
        )
        tool_events = [e for e in events if e.type == ClaudeEventType.TOOL_USE]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].metadata.get("file_path"), "out-copy")
        self.assertEqual(tool_events[0].metadata.get("bash_outputs"), ["out-copy"])

    def test_real_sdk_streamevent_object_is_dispatched(self) -> None:
        # РЕГРЕССИЯ: SDK отдаёт РАСПАРСЕННЫЙ объект StreamEvent (а не dict).
        # Раньше gate ловил только "stream_event" (dict) → реальные объекты
        # терялись и стрим не работал. Кормим настоящий объект.
        from claude_agent_sdk.types import StreamEvent

        bridge = ClaudeBridge()
        state = _SDKEventState()
        se = StreamEvent(
            uuid="u",
            session_id="s",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        )
        events = bridge._events_from_sdk_message(se, state)
        texts = [e.content for e in events if e.type == ClaudeEventType.TEXT and e.content]
        self.assertEqual(texts, ["Hello"])
        self.assertTrue(state.saw_stream_delta)
        # И собранный AssistantMessage после стрима — дубль, отбрасывается.
        self.assertEqual(self._run_state(bridge, state, [_assistant("Hello")]), [])

    def _run_state(self, bridge, state, raws):
        out = []
        for r in raws:
            out.extend(
                e for e in bridge._events_from_sdk_message(r, state)
                if e.type == ClaudeEventType.TEXT and e.content
            )
        return out

    def test_build_sdk_options_enables_partial_messages(self) -> None:
        captured: dict = {}

        class _Opts:
            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)

        bridge = ClaudeBridge()
        bridge._build_sdk_options(
            options_cls=_Opts,
            project_path="/tmp/x",
            session_id=None,
        )
        self.assertTrue(captured.get("include_partial_messages"))


if __name__ == "__main__":
    unittest.main()
