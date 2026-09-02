"""Детект «Claude разлогинен / OAuth-токен протух».

CLI печатает «Not logged in · Please run /login» как ОБЫЧНЫЙ вывод (не
исключение) — раньше он утекал в чат как ответ, и снаружи нельзя было отличить
«сервис упал» от «жив, но авторизация слетела». Теперь это ловится и
превращается в ЯВНУЮ ошибку авторизации (ERROR-событие) + лог.
"""
from __future__ import annotations

import unittest

from src.claude.bridge import (
    AUTH_EXPIRED_USER_MSG,
    ClaudeBridge,
    ClaudeEventType,
    _classify_error,
    _looks_like_auth_expired,
    _SDKEventState,
)


class AuthExpiredDetectTests(unittest.TestCase):
    def _run(self, raw: dict) -> list:
        bridge = ClaudeBridge()
        state = _SDKEventState()
        return bridge._events_from_sdk_message(raw, state)

    def test_marker_variants_detected(self) -> None:
        for s in (
            "Not logged in · Please run /login",
            "Invalid API key · Please run /login",
            "OAuth token has expired",
            "Please run /login to continue",
        ):
            self.assertTrue(_looks_like_auth_expired(s), s)

    def test_normal_text_not_flagged(self) -> None:
        # Обычный ответ, даже если упоминает /login в длинном тексте.
        long = (
            "Чтобы настроить вход, добавьте маршрут /login в роутер, обработайте "
            "форму, проверьте пароль и выдайте сессию. " * 4
        )
        self.assertFalse(_looks_like_auth_expired(long))
        self.assertFalse(_looks_like_auth_expired("Привет! Чем помочь?"))
        self.assertFalse(_looks_like_auth_expired(""))
        self.assertFalse(_looks_like_auth_expired(None))

    def test_result_message_auth_marker_becomes_error(self) -> None:
        events = self._run(
            {
                "type": "result",
                "is_error": False,
                "result": "Not logged in · Please run /login",
                "session_id": "s",
            }
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, ClaudeEventType.ERROR)
        self.assertEqual(events[0].content, AUTH_EXPIRED_USER_MSG)
        self.assertEqual(events[0].metadata.get("error_type"), "auth_expired")

    def test_assembled_message_auth_marker_becomes_error(self) -> None:
        events = self._run(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Invalid API key · Please run /login"}],
                    "model": "x",
                },
            }
        )
        self.assertEqual([e.type for e in events], [ClaudeEventType.ERROR])
        self.assertEqual(events[0].content, AUTH_EXPIRED_USER_MSG)

    def test_normal_result_not_converted(self) -> None:
        events = self._run(
            {"type": "result", "is_error": False, "result": "Готово.", "session_id": "s"}
        )
        self.assertEqual(events[0].type, ClaudeEventType.USAGE)

    def test_normal_assistant_text_passes_through(self) -> None:
        events = self._run(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Привет!"}], "model": "x"},
            }
        )
        self.assertEqual([e.type for e in events], [ClaudeEventType.TEXT])
        self.assertEqual(events[0].content, "Привет!")

    def test_classify_error_maps_login_message_to_auth(self) -> None:
        kind, _ = _classify_error(Exception("Not logged in · Please run /login"))
        self.assertEqual(kind, "auth")


if __name__ == "__main__":
    unittest.main()
