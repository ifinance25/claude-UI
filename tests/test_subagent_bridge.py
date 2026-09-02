from __future__ import annotations

import json
import unittest

from src.claude.bridge import ClaudeBridge, ClaudeEventType


class SubagentBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = ClaudeBridge()

    def test_parse_line_extracts_subagent_tool_activity(self) -> None:
        line = json.dumps(
            {
                "type": "subagent_event",
                "event": {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {
                        "file_path": "/tmp/project/src/app.py",
                    },
                },
            }
        )

        event = self.bridge._parse_line(line)

        self.assertIsNotNone(event)
        self.assertEqual(event.type, ClaudeEventType.SUBAGENT_LOG)
        self.assertEqual(event.content, "> 🔧 Read\n>    📂 src/app.py")

    def test_parse_line_marks_subagent_lifecycle(self) -> None:
        start = self.bridge._parse_line(
            json.dumps(
                {
                    "type": "subagent_event",
                    "event": {
                        "type": "start",
                        "name": "Researcher",
                    },
                }
            )
        )
        finish = self.bridge._parse_line(
            json.dumps(
                {
                    "type": "subagent_event",
                    "event": {
                        "type": "finish",
                        "name": "Researcher",
                    },
                }
            )
        )

        self.assertEqual(start.type, ClaudeEventType.SUBAGENT_START)
        self.assertEqual(finish.type, ClaudeEventType.SUBAGENT_FINISH)


if __name__ == "__main__":
    unittest.main()
