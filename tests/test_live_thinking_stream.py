"""Живой канал мыслей: bridge эмитит THINKING_DELTA параллельно с атомарным
блоком _flush_thinking, relay коалесцирует их в kind="thinking", не трогая
проверенный текстовый путь."""
from __future__ import annotations

import unittest

from src.claude.bridge import (
    THINKING_CLOSE_MARKER,
    THINKING_OPEN_MARKER,
    ClaudeBridge,
    ClaudeEvent,
    ClaudeEventType,
    _SDKEventState,
)
from src.event_bus import ClaudeEventRelay, EventBus
from src.event_bus.events import AgentFinished, AgentStreamingUpdate, UserMessageReceived

_OPEN = THINKING_OPEN_MARKER
_CLOSE = THINKING_CLOSE_MARKER


def _se(event: dict):
    from claude_agent_sdk.types import StreamEvent

    return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=None)


class BridgeStreamThinkingDeltaTests(unittest.TestCase):
    def _events(self, raw_messages: list):
        bridge = ClaudeBridge()
        state = _SDKEventState()
        out = []
        for rm in raw_messages:
            out.extend(bridge._events_from_sdk_message(rm, state))
        return out

    def test_thinking_deltas_emitted_live_and_block_still_atomic(self) -> None:
        raw = [
            _se({"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": ""}}),
            _se({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "часть один "}}),
            _se({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "часть два"}}),
            _se({"type": "content_block_stop", "index": 0}),
        ]
        events = self._events(raw)
        # Живые дельты — отдельным типом, с сырым текстом каждой дельты.
        deltas = [e.content for e in events if e.type == ClaudeEventType.THINKING_DELTA]
        self.assertEqual(deltas, ["часть один ", "часть два"])
        # Живые дельты — сырой текст БЕЗ маркеров (страж исторической утечки мыслей).
        self.assertTrue(all(_OPEN not in d and _CLOSE not in d for d in deltas))
        # Атомарный блок по-прежнему ровно один TEXT-ивент с парой маркеров.
        marker_texts = [
            e.content for e in events
            if e.type == ClaudeEventType.TEXT and (_OPEN in e.content or _CLOSE in e.content)
        ]
        self.assertEqual(marker_texts, [f"{_OPEN}часть один часть два{_CLOSE}\n\n"])

    def test_start_block_with_inline_thinking_emits_delta(self) -> None:
        raw = [
            _se({"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": "стартовая мысль"}}),
            _se({"type": "content_block_stop", "index": 0}),
        ]
        events = self._events(raw)
        deltas = [e.content for e in events if e.type == ClaudeEventType.THINKING_DELTA]
        self.assertIn("стартовая мысль", deltas)


class BridgeAssembledThinkingTests(unittest.TestCase):
    def test_assembled_thinking_block_emits_delta_and_atomic(self) -> None:
        bridge = ClaudeBridge()
        # Собранный (не partial) контент-блок мысли — как на CLI/tmux-фолбэке.
        # saw_stream_delta=False (дефолт) → это именно сборочная ветка.
        state = _SDKEventState()
        block = {"type": "thinking", "thinking": "цельная мысль"}
        events = bridge._events_from_content_blocks([block], state)
        types = [e.type for e in events]
        self.assertIn(ClaudeEventType.THINKING_DELTA, types)
        delta = next(e for e in events if e.type == ClaudeEventType.THINKING_DELTA)
        self.assertEqual(delta.content, "цельная мысль")
        # Атомарный TEXT-блок с маркерами тоже присутствует.
        text = next(
            e for e in events
            if e.type == ClaudeEventType.TEXT and _OPEN in e.content
        )
        self.assertEqual(text.content, f"{_OPEN}цельная мысль{_CLOSE}\n\n")


class _FakeBridge:
    def __init__(self, events):
        self.events = events

    async def send_message(self, **kwargs):
        for e in self.events:
            yield e


class RelayThinkingCoalesceTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, events):
        bus = EventBus()
        updates: list[AgentStreamingUpdate] = []
        finished: list[AgentFinished] = []

        async def collect(ev: AgentStreamingUpdate) -> None:
            updates.append(ev)

        async def collect_finished(ev: AgentFinished) -> None:
            finished.append(ev)

        bus.subscribe(AgentStreamingUpdate, collect)
        bus.subscribe(AgentFinished, collect_finished)
        relay = ClaudeEventRelay(bus=bus, claude_bridge=_FakeBridge(events))
        await bus.publish(
            UserMessageReceived(
                request_id="r1", chat_id=1, topic_id=1,
                project_path="/x", text="hi", session_uuid="s1",
            )
        )
        relay.close()
        return updates, (finished[-1] if finished else None)

    async def test_thinking_deltas_coalesced_but_complete(self) -> None:
        events = [ClaudeEvent(ClaudeEventType.THINKING_DELTA, "мысль") for _ in range(50)]
        events.append(ClaudeEvent(ClaudeEventType.COMPLETE, metadata={"usage": {}, "session_id": "s"}))
        updates, fin = await self._run(events)
        think = [u for u in updates if u.kind == "thinking"]
        self.assertEqual("".join(u.content for u in think), "мысль" * 50)
        self.assertLess(len(think), 50)  # реально склеено
        # Главный инвариант: мысли НЕ утекают в финальный ответ/историю.
        self.assertIsNotNone(fin)
        self.assertNotIn("мысль", fin.response_text)

    async def test_thinking_flushed_before_tool_and_text_order(self) -> None:
        events = [
            ClaudeEvent(ClaudeEventType.THINKING_DELTA, "размышляю "),  # < 40 → копится
            ClaudeEvent(ClaudeEventType.TOOL_USE, "🔧 Read", metadata={"name": "Read"}),
            ClaudeEvent(ClaudeEventType.TEXT, "ответ"),
            ClaudeEvent(ClaudeEventType.COMPLETE, metadata={"usage": {}, "session_id": "s"}),
        ]
        updates, _fin = await self._run(events)
        kinds = [u.kind for u in updates]
        self.assertEqual(kinds[0], "thinking")
        self.assertEqual(updates[0].content, "размышляю ")
        self.assertEqual(kinds[1], "tool_use")
        self.assertEqual("".join(u.content for u in updates if u.kind == "text"), "ответ")
        self.assertEqual("".join(u.content for u in updates if u.kind == "thinking"), "размышляю ")


if __name__ == "__main__":
    unittest.main()
