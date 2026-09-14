"""Системный промпт Claude: всегда — инструкция «отвечай и думай по-русски»;
плюс публичный адрес веба, если задан (issue #2)."""
from __future__ import annotations

import unittest

from src.claude.bridge import ClaudeBridge


def _build(bridge: ClaudeBridge) -> dict:
    captured: dict = {}

    class _Opts:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    bridge._build_sdk_options(
        options_cls=_Opts, project_path="/tmp/x", session_id=None
    )
    return captured


class WebOriginSystemPromptTests(unittest.TestCase):
    def test_appends_public_origin(self):
        bridge = ClaudeBridge(web_public_origin="https://ai-panel.example.com")
        kwargs = _build(bridge)
        sp = kwargs.get("system_prompt")
        self.assertIsInstance(sp, dict)
        self.assertEqual(sp["type"], "preset")
        self.assertEqual(sp["preset"], "claude_code")
        self.assertIn("ai-panel.example.com", sp["append"])
        # Русская инструкция всегда присутствует рядом с адресом.
        self.assertIn("РУССКОМ", sp["append"])

    def test_russian_instruction_always_present(self):
        # Даже БЕЗ публичного адреса системный промпт ставится — с инструкцией
        # вести размышления и ответы на русском (мысли Claude по умолчанию англ.).
        bridge = ClaudeBridge()
        kwargs = _build(bridge)
        sp = kwargs.get("system_prompt")
        self.assertIsInstance(sp, dict)
        self.assertEqual(sp["preset"], "claude_code")
        self.assertIn("РУССКОМ", sp["append"])
        self.assertNotIn("ai-panel.example.com", sp["append"])

    def test_blank_origin_still_russian_only(self):
        bridge = ClaudeBridge(web_public_origin="   ")
        kwargs = _build(bridge)
        sp = kwargs.get("system_prompt")
        self.assertIsInstance(sp, dict)
        self.assertIn("РУССКОМ", sp["append"])
        # Пустой origin не подмешивает адрес.
        self.assertNotIn("http", sp["append"])


if __name__ == "__main__":
    unittest.main()
