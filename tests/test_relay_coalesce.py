"""ClaudeEventRelay коалесцирует text-дельты, ничего не теряя, и сбрасывает
накопленный текст ПЕРЕД не-text событиями (issue #7)."""
from __future__ import annotations

import unittest

from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.event_bus import ClaudeEventRelay, EventBus
from src.event_bus.events import AgentStreamingUpdate, UserMessageReceived


class _FakeBridge:
    def __init__(self, events: list[ClaudeEvent]) -> None:
        self.events = events

    async def send_message(self, **kwargs):
        for e in self.events:
            yield e


class RelayCoalesceTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, events: list[ClaudeEvent]) -> list[AgentStreamingUpdate]:
        bus = EventBus()
        updates: list[AgentStreamingUpdate] = []

        async def collect(ev: AgentStreamingUpdate) -> None:
            updates.append(ev)

        bus.subscribe(AgentStreamingUpdate, collect)
        relay = ClaudeEventRelay(bus=bus, claude_bridge=_FakeBridge(events))
        await bus.publish(
            UserMessageReceived(
                request_id="r1",
                chat_id=1,
                topic_id=1,
                project_path="/x",
                text="hi",
                session_uuid="s1",
            )
        )
        relay.close()
        return updates

    async def test_text_coalesced_but_complete(self) -> None:
        events = [ClaudeEvent(ClaudeEventType.TEXT, "abcde") for _ in range(50)]
        events.append(
            ClaudeEvent(ClaudeEventType.COMPLETE, metadata={"usage": {}, "session_id": "s"})
        )
        updates = await self._run(events)
        texts = [u for u in updates if u.kind == "text"]
        joined = "".join(u.content for u in texts)
        self.assertEqual(joined, "abcde" * 50)  # ничего не потеряно
        self.assertLess(len(texts), 50)  # реально склеено в меньшее число событий

    async def test_non_text_flushes_pending_in_order(self) -> None:
        events = [
            ClaudeEvent(ClaudeEventType.TEXT, "short"),  # < 40 → сам не флашится
            ClaudeEvent(ClaudeEventType.TOOL_USE, "🔧 Bash", metadata={"name": "Bash"}),
            ClaudeEvent(ClaudeEventType.TEXT, "after"),
            ClaudeEvent(ClaudeEventType.COMPLETE, metadata={"usage": {}, "session_id": "s"}),
        ]
        updates = await self._run(events)
        kinds = [u.kind for u in updates]
        self.assertEqual(kinds[0], "text")
        self.assertEqual(updates[0].content, "short")  # текст сброшен ПЕРЕД tool_use
        self.assertEqual(kinds[1], "tool_use")
        # «after» тоже доходит (полнота)
        self.assertEqual("".join(u.content for u in updates if u.kind == "text"), "shortafter")


if __name__ == "__main__":
    unittest.main()
