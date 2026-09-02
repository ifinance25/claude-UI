from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.bot.handlers import files
from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.claude.session import TopicSession
from src.event_bus import EventBus, UserMessageReceived


class _FakeStreamer:
    def __init__(self) -> None:
        self.state = object()
        self.streamed: list[str] = []
        self.finalized: list[dict] = []

    async def create_log_message(self, *, chat_id: int, topic_id: int):
        return self.state

    async def update_log(self, state, content: str) -> None:
        return None

    async def stream_response(self, state, content: str) -> None:
        self.streamed.append(content)

    async def finalize(self, state, *, show_token_usage: bool, usage=None, show_context_usage: bool = False, keep_log: bool = False) -> None:
        self.finalized.append(
            {
                "state": state,
                "show_token_usage": show_token_usage,
                "usage": usage,
            }
        )


class _FakeBridge:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def check_auth(self) -> bool:
        return True

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        yield ClaudeEvent(ClaudeEventType.TEXT, "done")
        yield ClaudeEvent(
            ClaudeEventType.COMPLETE,
            metadata={"usage": {"output_tokens": 1}, "session_id": "updated-session"},
        )


class ImageAnalysisHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.streamer = _FakeStreamer()
        self.bridge = _FakeBridge()
        self.bot = SimpleNamespace()
        self.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="image/file.png"))
        self.bot.download_file = AsyncMock(side_effect=self._write_downloaded_image)
        self.bot.send_chat_action = AsyncMock()

        self.session = TopicSession(
            topic_id=99,
            session_id="session-1",
            project_path=self.tmpdir.name,
            project_name="demo",
        )
        self.session_manager = SimpleNamespace(
            get_session=lambda topic_id: self.session,
            async_get_session=AsyncMock(return_value=self.session),
            update_session_id=Mock(),
            async_update_session_id=AsyncMock(),
            async_set_status=AsyncMock(),
            async_mark_renamed=AsyncMock(),
            async_add_usage=AsyncMock(),
            async_get_usage=AsyncMock(return_value={}),
            async_clear_session_id=AsyncMock(),
        )

        files.router.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False, show_context_usage=False, keep_log_after_response=False),
            get_light_project_paths=lambda: [],
            get_allowed_user_ids=lambda: [],
            require_user_key=False,
        )
        files.router.session_manager = self.session_manager
        files.router.claude_bridge = self.bridge
        files.router.streamer = self.streamer
        files.router.bot = self.bot
        files.router.event_bus = None
        # Gate dormant (no key store) → direct-bridge path uses owner creds.
        files.router.api_key_store = None

    async def _write_downloaded_image(self, file_path: str, destination) -> None:
        destination.write(b"image-bytes")
        destination.flush()

    async def test_photo_upload_uses_image_analysis_attachment(self) -> None:
        message = SimpleNamespace(
            photo=[
                SimpleNamespace(file_id="small-photo", file_size=10, width=64, height=64),
                SimpleNamespace(file_id="large-photo", file_size=42, width=1280, height=720),
            ],
            caption="Slice this layout for me",
            reply_to_message=SimpleNamespace(text="Previous context", caption=None),
            chat=SimpleNamespace(id=1234),
            message_thread_id=99,
            from_user=SimpleNamespace(username="tester", id=1166057082),
            answer=AsyncMock(),
        )

        await files.handle_photo(message)

        self.assertEqual(len(self.bridge.messages), 1)
        sent = self.bridge.messages[0]
        self.assertIn("attachments", sent)
        self.assertEqual(len(sent["attachments"]), 1)

        attachment = sent["attachments"][0]
        self.assertEqual(attachment.kind, "screenshot")
        self.assertEqual(attachment.mime_type, "image/jpeg")
        self.assertEqual(attachment.base64_data, base64.b64encode(b"image-bytes").decode())
        # Windows-safe: backslashes -> forward slashes for the path
        # shape assertion; the file existence check uses Path which
        # accepts both natively.
        self.assertEqual(
            attachment.source_path.replace("\\", "/"),
            ".telegram-uploads/images/screenshot.jpg",
        )
        self.assertTrue((Path(self.tmpdir.name) / attachment.source_path).exists())
        self.assertIn("Previous context", sent["message"])
        self.assertIn("Slice this layout for me", sent["message"])
        self.assertIn(attachment.source_path, sent["message"])
        self.assertEqual(self.streamer.streamed, ["done"])

    async def test_image_document_upload_uses_diagram_classification(self) -> None:
        message = SimpleNamespace(
            document=SimpleNamespace(
                file_id="doc-photo",
                file_size=99,
                file_name="diagram.png",
                mime_type="image/png",
            ),
            caption="diagram of the flow",
            reply_to_message=None,
            chat=SimpleNamespace(id=1234),
            message_thread_id=99,
            from_user=SimpleNamespace(username="tester", id=1166057082),
            answer=AsyncMock(),
        )

        await files.handle_document(message)

        self.assertEqual(len(self.bridge.messages), 1)
        sent = self.bridge.messages[0]
        attachment = sent["attachments"][0]
        self.assertEqual(attachment.kind, "diagram")
        self.assertEqual(attachment.mime_type, "image/png")
        self.assertEqual(
            attachment.base64_data,
            base64.b64encode(b"image-bytes").decode(),
        )
        # Windows-safe path shape comparison.
        self.assertEqual(
            attachment.source_path.replace("\\", "/"),
            ".telegram-uploads/images/diagram.png",
        )
        self.assertTrue((Path(self.tmpdir.name) / attachment.source_path).exists())
        self.assertIn("diagram of the flow", sent["message"])


class ImageUploadPrivilegeTests(unittest.IsolatedAsyncioTestCase):
    """The event-bus publish site must resolve the sender's privilege so the
    SP2 relay does not wrongly refuse an admin's file/image upload."""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.streamer = _FakeStreamer()
        self.bot = SimpleNamespace()
        self.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="image/file.png"))
        self.bot.download_file = AsyncMock(side_effect=self._write_downloaded_image)
        self.bot.send_chat_action = AsyncMock()

        self.session = TopicSession(
            topic_id=99,
            session_id="session-1",
            project_path=self.tmpdir.name,
            project_name="demo",
        )

        self.event_bus = EventBus()
        self.published: list[UserMessageReceived] = []

        async def _capture(event: UserMessageReceived) -> None:
            self.published.append(event)

        self.event_bus.subscribe(UserMessageReceived, _capture)

    def _make_session_manager(self, *, is_admin: int) -> SimpleNamespace:
        return SimpleNamespace(
            get_session=lambda topic_id: self.session,
            async_get_session=AsyncMock(return_value=self.session),
            update_session_id=Mock(),
            async_update_session_id=AsyncMock(),
            async_set_status=AsyncMock(),
            async_mark_renamed=AsyncMock(),
            async_add_usage=AsyncMock(),
            async_get_usage=AsyncMock(return_value={}),
            async_clear_session_id=AsyncMock(),
            get_user_by_id=lambda uid: {"id": uid, "is_admin": is_admin},
        )

    def _wire_router(self, session_manager, *, allowed_ids: tuple[int, ...] = ()) -> None:
        files.router.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False, show_context_usage=False, keep_log_after_response=False),
            get_light_project_paths=lambda: [],
            get_allowed_user_ids=lambda: list(allowed_ids),
        )
        files.router.session_manager = session_manager
        files.router.claude_bridge = SimpleNamespace(check_auth=AsyncMock(return_value=True))
        files.router.streamer = self.streamer
        files.router.bot = self.bot
        files.router.event_bus = self.event_bus

    async def _write_downloaded_image(self, file_path: str, destination) -> None:
        destination.write(b"image-bytes")
        destination.flush()

    def _make_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            document=SimpleNamespace(
                file_id="doc-photo",
                file_size=99,
                file_name="diagram.png",
                mime_type="image/png",
            ),
            caption="diagram of the flow",
            reply_to_message=None,
            chat=SimpleNamespace(id=1234),
            message_thread_id=99,
            from_user=SimpleNamespace(username="tester", id=1166057082),
            answer=AsyncMock(),
        )

    async def test_admin_upload_publishes_privileged_event(self) -> None:
        self._wire_router(self._make_session_manager(is_admin=1))

        await files.handle_document(self._make_message())

        self.assertEqual(len(self.published), 1)
        event = self.published[0]
        self.assertTrue(event.privileged)
        self.assertEqual(event.user_id, 1166057082)

    async def test_non_admin_upload_publishes_unprivileged_event(self) -> None:
        self._wire_router(self._make_session_manager(is_admin=0))

        await files.handle_document(self._make_message())

        self.assertEqual(len(self.published), 1)
        self.assertFalse(self.published[0].privileged)

    async def test_whitelisted_owner_upload_publishes_privileged_event(self) -> None:
        # Owner (is_admin=0 after Telegram login) but in ALLOWED_USER_IDS: the
        # file upload must publish a privileged event so the SP2 relay does not
        # wrongly refuse it. User id 1166057082 matches the whitelist here.
        self._wire_router(
            self._make_session_manager(is_admin=0),
            allowed_ids=(1166057082,),
        )

        await files.handle_document(self._make_message())

        self.assertEqual(len(self.published), 1)
        self.assertTrue(self.published[0].privileged)


class _FakeStore:
    """Minimal ApiKeyStore stand-in: returns a fixed key for any user."""

    def __init__(self, key: str | None) -> None:
        self._key = key
        self.lookups: list[int] = []

    def get_key(self, user_id: int) -> str | None:
        self.lookups.append(user_id)
        return self._key


class DirectBridgeAuthGateTests(unittest.IsolatedAsyncioTestCase):
    """SP2 auth gate on the files direct-bridge fallback (event_bus unset).

    Production always wires event_bus, so the relay applies the gate and this
    path is dead. But if a future change reaches it, an upload must NOT spawn on
    the OWNER's credentials: a stored user key is injected, and an unprivileged
    caller with no key under require_user_key is refused without spawning Claude.
    """

    USER_KEY = "sk-ant-api03-FILESkey1234"

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.streamer = _FakeStreamer()
        self.bridge = _FakeBridge()
        self.bot = SimpleNamespace()
        self.bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="doc/file.txt"))
        self.bot.download_file = AsyncMock(side_effect=self._write_downloaded)
        self.bot.send_chat_action = AsyncMock()

        self.session = TopicSession(
            topic_id=99,
            session_id="session-1",
            project_path=self.tmpdir.name,
            project_name="demo",
        )

    async def _write_downloaded(self, file_path: str, destination) -> None:
        destination.write(b"doc-bytes")
        destination.flush()

    def _make_session_manager(self, *, is_admin: int = 0) -> SimpleNamespace:
        return SimpleNamespace(
            get_session=lambda topic_id: self.session,
            async_get_session=AsyncMock(return_value=self.session),
            update_session_id=Mock(),
            async_update_session_id=AsyncMock(),
            async_set_status=AsyncMock(),
            async_mark_renamed=AsyncMock(),
            async_add_usage=AsyncMock(),
            async_get_usage=AsyncMock(return_value={}),
            async_clear_session_id=AsyncMock(),
            get_user_by_id=lambda uid: {"id": uid, "is_admin": is_admin},
        )

    def _wire(
        self,
        *,
        store,
        require_user_key: bool,
        is_admin: int = 0,
        allowed_ids: tuple[int, ...] = (),
    ) -> None:
        files.router.settings = SimpleNamespace(
            display=SimpleNamespace(show_logs=False, show_token_usage=False, show_context_usage=False, keep_log_after_response=False),
            get_light_project_paths=lambda: [],
            get_allowed_user_ids=lambda: list(allowed_ids),
            require_user_key=require_user_key,
        )
        files.router.session_manager = self._make_session_manager(is_admin=is_admin)
        files.router.claude_bridge = self.bridge
        files.router.streamer = self.streamer
        files.router.bot = self.bot
        files.router.event_bus = None  # direct-bridge fallback path
        files.router.api_key_store = store

    def _make_message(self) -> SimpleNamespace:
        return SimpleNamespace(
            document=SimpleNamespace(
                file_id="doc-1",
                file_size=99,
                file_name="notes.txt",
                mime_type="text/plain",
            ),
            caption="analyze this",
            reply_to_message=None,
            chat=SimpleNamespace(id=1234),
            message_thread_id=99,
            from_user=SimpleNamespace(username="student", id=222),
            answer=AsyncMock(),
        )

    async def test_direct_bridge_injects_stored_user_key(self) -> None:
        # event_bus=None, unprivileged caller with a stored key → USER_KEY mode:
        # the caller's key must reach send_message (per-user billing preserved).
        self._wire(store=_FakeStore(self.USER_KEY), require_user_key=True, is_admin=0)

        message = self._make_message()
        await files.handle_document(message)

        self.assertEqual(len(self.bridge.messages), 1)
        self.assertEqual(self.bridge.messages[0]["anthropic_api_key"], self.USER_KEY)
        message.answer.assert_not_awaited()

    async def test_direct_bridge_refuses_unprivileged_without_key(self) -> None:
        # event_bus=None, unprivileged, no key, require_user_key active → refused:
        # Claude must NOT be spawned on the owner's credentials.
        self._wire(store=_FakeStore(None), require_user_key=True, is_admin=0)

        message = self._make_message()
        await files.handle_document(message)

        self.assertEqual(self.bridge.messages, [])  # no owner-cred spawn
        message.answer.assert_awaited_once()
        (sent_text,) = message.answer.await_args.args
        self.assertIn("/apikey", sent_text)


if __name__ == "__main__":
    unittest.main()
