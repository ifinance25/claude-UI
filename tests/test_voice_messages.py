from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from pathlib import Path as PathLib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.bot.handlers import files
from src.bot.handlers import messages


class VoiceMessageHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.streamer = SimpleNamespace()
        self.bridge = SimpleNamespace(check_auth=AsyncMock(return_value=True))
        self.bot = SimpleNamespace()
        self.bot.get_file = AsyncMock(
            return_value=SimpleNamespace(file_path="voice/file.ogg")
        )
        self.bot.download_file = AsyncMock(side_effect=self._write_downloaded_voice)
        self.bot.send_chat_action = AsyncMock()
        self.bot.create_forum_topic = AsyncMock(
            return_value=SimpleNamespace(message_thread_id=321)
        )
        self.bot.send_message = AsyncMock()
        self.bot.get_forum_topic_icon_stickers = AsyncMock(return_value=[])

        self.session_manager = SimpleNamespace(
            get_session=Mock(return_value=None),
            update_session_id=Mock(),
            # Light привязывает единственный проект сам — хендлер зовёт создание
            # сессии вместо показа клавиатуры выбора.
            async_create_session=AsyncMock(),
            async_get_session=AsyncMock(return_value=None),
        )

        self.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False),
            get_light_project_paths=lambda: [PathLib("demo-project")],
        )
        for router in (files.router, messages.router):
            router.settings = self.settings  # type: ignore[attr-defined]
            router.session_manager = self.session_manager  # type: ignore[attr-defined]
            router.claude_bridge = self.bridge  # type: ignore[attr-defined]
            router.streamer = self.streamer  # type: ignore[attr-defined]
            router.bot = self.bot  # type: ignore[attr-defined]
            router.event_bus = None  # type: ignore[attr-defined]

    async def _write_downloaded_voice(self, file_path: str, destination) -> None:
        destination.write(b"ogg-bytes")
        destination.flush()

    async def test_process_incoming_text_creates_topic_for_general_chat(self) -> None:
        message = SimpleNamespace(
            text="Сделай краткое summary",
            chat=SimpleNamespace(id=1234),
            message_thread_id=None,
            from_user=SimpleNamespace(username="tester", id=7),
            answer=AsyncMock(),
        )

        await messages.process_incoming_text(message, message.text)

        self.bot.create_forum_topic.assert_awaited_once()
        # Клавиатуры выбора нет: light привязывает единственный проект сам.
        self.bot.send_message.assert_awaited_once_with(
            chat_id=1234,
            message_thread_id=321,
            text=(
                "<b>Новая сессия Vels Claude Light</b>\n\n"
                "Топик создан автоматически, проект подключён.\n\n"
                "Отправьте первую задачу, файл, скриншот или команду Claude."
            ),
            parse_mode="HTML",
        )
        message.answer.assert_not_awaited()

    async def test_voice_message_transcribes_and_delegates_to_shared_text_flow(self) -> None:
        message = SimpleNamespace(
            voice=SimpleNamespace(file_id="voice-file-id", file_size=42),
            caption="Сделай краткое summary",
            reply_to_message=SimpleNamespace(text="Контекст из ответа", caption=None),
            chat=SimpleNamespace(id=1234),
            message_thread_id=None,
            from_user=SimpleNamespace(username="tester"),
            answer=AsyncMock(),
        )

        temp_paths: list[Path] = []

        async def fake_transcribe(path: Path) -> str:
            temp_paths.append(path)
            self.assertTrue(path.exists())
            return "Recognized voice text"

        with patch(
            "src.bot.handlers.messages.process_incoming_text",
            new=AsyncMock(),
        ) as process_incoming_text:
            with patch("src.bot.handlers.files._transcribe_voice_message", side_effect=fake_transcribe):
                await files.handle_voice(message)

        self.bot.send_chat_action.assert_awaited_once_with(
            1234,
            action="typing",
            message_thread_id=None,
        )
        process_incoming_text.assert_awaited_once_with(
            message,
            "Контекст ответа:\nКонтекст из ответа\n\n"
            "Сделай краткое summary\n\n"
            "Recognized voice text",
        )
        self.assertTrue(temp_paths)
        self.assertFalse(temp_paths[0].exists())
        message.answer.assert_not_awaited()

    async def test_voice_message_reports_missing_whisper(self) -> None:
        message = SimpleNamespace(
            voice=SimpleNamespace(file_id="voice-file-id", file_size=42),
            caption=None,
            reply_to_message=None,
            chat=SimpleNamespace(id=1234),
            message_thread_id=None,
            from_user=SimpleNamespace(username="tester"),
            answer=AsyncMock(),
        )

        with patch("src.bot.handlers.messages.process_incoming_text", new=AsyncMock()) as process_incoming_text:
            with patch(
                "src.bot.handlers.files._transcribe_voice_message",
                side_effect=files.WhisperCLIUnavailableError("whisper"),
            ):
                await files.handle_voice(message)

        process_incoming_text.assert_not_awaited()
        message.answer.assert_awaited_once_with(
            "Whisper CLI не найден на сервере.\n"
            "Установите: <code>pip install openai-whisper</code>",
            parse_mode="HTML",
        )


if __name__ == "__main__":
    unittest.main()
