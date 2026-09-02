"""Регрессия: утечка «мыслей» из-за интерливленных пустых SystemMessage.

Корень бага (воспроизведён на живых данных прода): SDK при extended thinking
интерливит каждую `thinking_delta` ПУСТЫМ `SystemMessage`. Bridge превращал
каждый такой SystemMessage в LOG-событие (не-text) → релай флашил текстовый
буфер на каждом → открывающий THINKING-маркер отрывался в отдельный
streaming_update/бабл, мышление дробилось на куски, а пара OPEN…CLOSE на фронте
ломалась. Итог: сырой английский текст «мыслей» + литеральный `□THINKING□` +
пустые блоки в чате.

Этот тест моделирует ВЕСЬ путь end-to-end: реальные SDK-объекты →
`_events_from_sdk_message` → коалесцинг релая (TEXT_FLUSH_CHARS) → склейка
баблов на фронте (text мерджится только с предыдущим text) → порт
`extractThinking`/`stripThinking` из web/src/lib/stripThinking.ts. Старый тест
этого не ловил, потому что брал ОДИН склеенный буфер и не моделировал ни
интерлив, ни по-бабловый рендер.
"""
from __future__ import annotations

import re
import unittest

from src.claude.bridge import (
    THINKING_CLOSE_MARKER,
    THINKING_OPEN_MARKER,
    ClaudeBridge,
    ClaudeEventType,
    _SDKEventState,
)

# Зеркало src/event_bus/claude.py.
TEXT_FLUSH_CHARS = 40

_OPEN = THINKING_OPEN_MARKER
_CLOSE = THINKING_CLOSE_MARKER

# --- порт фронтового web/src/lib/stripThinking.ts ---
_PAIR_RE = re.compile(re.escape(_OPEN) + r"[\s\S]*?" + re.escape(_CLOSE) + r"\n*")
_ORPHAN_LEAD_RE = re.compile(r"^[\s\S]*?" + re.escape(_CLOSE) + r"\n*")
_TRAIL_OPEN_RE = re.compile(re.escape(_OPEN) + r"[\s\S]*$")
_LONE_RE = re.compile(re.escape(_OPEN) + "|" + re.escape(_CLOSE))
_CTRL_RE = re.compile("[\x01\x02]")
_PAIR_CAPTURE_RE = re.compile(re.escape(_OPEN) + r"([\s\S]*?)" + re.escape(_CLOSE))


def _strip_thinking(raw: str) -> str:
    raw = _PAIR_RE.sub("", raw)
    raw = _ORPHAN_LEAD_RE.sub("", raw)
    raw = _TRAIL_OPEN_RE.sub("", raw)
    raw = _LONE_RE.sub("", raw)
    raw = _CTRL_RE.sub("", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _extract_thinking(raw: str) -> list[str]:
    return [m.group(1).strip() for m in _PAIR_CAPTURE_RE.finditer(raw) if m.group(1).strip()]


def _se(event: dict):
    from claude_agent_sdk.types import StreamEvent

    return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=None)


def _sysmsg():
    # Реальный интерливленный пустой SystemMessage (без .message → content "").
    from claude_agent_sdk.types import SystemMessage

    return SystemMessage(subtype="status", data={})


class ThinkingInterleaveRegressionTests(unittest.TestCase):
    def _bridge_events(self, raw_messages: list):
        bridge = ClaudeBridge()
        state = _SDKEventState()
        out = []
        for rm in raw_messages:
            out.extend(bridge._events_from_sdk_message(rm, state))
        return out

    def _render_frontend(self, claude_events: list):
        """Симулирует релай (коалесцинг) + фронт (склейка баблов + extract).

        Возвращает (visible_joined, thinking_blocks, empty_log_bubbles).
        """
        # 1) релай: TEXT копится до TEXT_FLUSH_CHARS, не-text флашит и сам идёт.
        updates: list[tuple[str, str]] = []
        pending = ""

        def flush():
            nonlocal pending
            if pending:
                updates.append(("text", pending))
                pending = ""

        for ev in claude_events:
            if ev.type == ClaudeEventType.TEXT:
                pending += ev.content or ""
                if len(pending) >= TEXT_FLUSH_CHARS:
                    flush()
            elif ev.type == ClaudeEventType.USAGE:
                # usage не создаёт бабл, на склейку текста не влияет.
                flush()
            else:
                flush()
                kind = "log" if ev.type == ClaudeEventType.LOG else ev.type.name.lower()
                updates.append((kind, ev.content or ""))
        flush()

        # 2) фронт (Chat.tsx): text мерджится только с предыдущим text-баблом.
        bubbles: list[list[str]] = []
        for kind, content in updates:
            if kind == "text" and bubbles and bubbles[-1][0] == "text":
                bubbles[-1][1] += content
            else:
                bubbles.append([kind, content])

        visible_parts: list[str] = []
        thinking: list[str] = []
        empty_logs = 0
        for kind, content in bubbles:
            if kind == "log":
                if not content.strip():
                    empty_logs += 1
                continue
            if kind != "text":
                continue
            v = _strip_thinking(content)
            if v.strip():
                visible_parts.append(v)
            thinking.extend(_extract_thinking(content))
        return "\n".join(visible_parts), thinking, empty_logs

    def test_interleaved_system_messages_do_not_leak_thinking(self) -> None:
        think1 = "The user greeted me in Russian. "
        think2 = "Simple conversational question, so I respond naturally."
        answer = "Привет! Я Claude Code — помогаю с кодом прямо из терминала."
        raw = [
            _se({"type": "message_start", "message": {}}),
            _se({"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": ""}}),
            _sysmsg(),
            _se({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": think1}}),
            _sysmsg(),
            _se({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": think2}}),
            _sysmsg(),
            _se({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "signature_delta", "signature": "sig"}}),
            _se({"type": "content_block_stop", "index": 0}),
            _se({"type": "content_block_start", "index": 1,
                 "content_block": {"type": "text", "text": ""}}),
            _se({"type": "content_block_delta", "index": 1,
                 "delta": {"type": "text_delta", "text": answer}}),
            _se({"type": "content_block_stop", "index": 1}),
        ]
        events = self._bridge_events(raw)
        visible, thinking, empty_logs = self._render_frontend(events)

        # Ни маркеров, ни управляющих байтов, ни сырых англ. «мыслей» в видимом.
        self.assertNotIn(_OPEN, visible)
        self.assertNotIn(_CLOSE, visible)
        self.assertNotIn("\x01", visible)
        self.assertNotIn("\x02", visible)
        self.assertNotIn("greeted me", visible)
        self.assertNotIn("respond naturally", visible)
        # Видимый текст = ровно ответ.
        self.assertEqual(visible.strip(), answer)
        # Мышление сохранено и собрано в ОДИН блок «Размышления».
        self.assertEqual(thinking, [think1 + think2])
        # Пустых LOG-баблов (тех самых «пустых блоков» в чате) нет.
        self.assertEqual(empty_logs, 0)

    def test_empty_system_message_emits_no_log_event(self) -> None:
        bridge = ClaudeBridge()
        state = _SDKEventState()
        # И объектная форма, и dict-форма пустого системного сообщения.
        self.assertEqual(bridge._events_from_sdk_message(_sysmsg(), state), [])
        self.assertEqual(
            bridge._events_from_sdk_message({"type": "system", "subtype": "status"}, state),
            [],
        )

    def test_nonempty_system_message_still_logged(self) -> None:
        bridge = ClaudeBridge()
        state = _SDKEventState()
        events = bridge._events_from_sdk_message(
            {"type": "system", "subtype": "note", "message": "важное"}, state
        )
        self.assertEqual([e.type for e in events], [ClaudeEventType.LOG])
        self.assertEqual(events[0].content, "важное")

    def test_thinking_emitted_atomically_in_single_event(self) -> None:
        # Маркеры мышления должны быть В ОДНОМ событии (не разорваться).
        raw = [
            _se({"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": ""}}),
            _sysmsg(),
            _se({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "часть один "}}),
            _sysmsg(),
            _se({"type": "content_block_delta", "index": 0,
                 "delta": {"type": "thinking_delta", "thinking": "часть два"}}),
            _se({"type": "content_block_stop", "index": 0}),
        ]
        events = self._bridge_events(raw)
        text_events = [e.content for e in events if e.type == ClaudeEventType.TEXT and e.content]
        # Ровно одно событие, содержащее и OPEN, и CLOSE рядом со своим текстом.
        marker_events = [t for t in text_events if _OPEN in t or _CLOSE in t]
        self.assertEqual(len(marker_events), 1)
        self.assertEqual(
            marker_events[0],
            f"{_OPEN}часть один часть два{_CLOSE}\n\n",
        )

    def test_legacy_orphan_close_is_stripped_on_frontend(self) -> None:
        # Старая сломанная история: сырой текст + осиротевший CLOSE + ответ.
        broken = f"raw english thinking{_CLOSE}\n\nНастоящий ответ."
        self.assertEqual(_strip_thinking(broken), "Настоящий ответ.")
        self.assertNotIn(_CLOSE, _strip_thinking(broken))


if __name__ == "__main__":
    unittest.main()
