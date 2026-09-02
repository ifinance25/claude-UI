"""Integration tests for the SP2 auth gate in the Telegram message handler.

Mirrors :mod:`tests.integration.event_bus.test_claude_relay` on the bot side.
Before dispatching a user message to Claude, ``process_incoming_text`` resolves
*which credentials* the session runs against (see
:func:`src.apikeys.policy.resolve_session_auth`). These tests drive the handler
through its direct-bridge path (``event_bus`` unset) with a fake bridge/store and
assert the key that reaches ``bridge.send_message`` — or, for the refusal path,
that no run happens and the ``/apikey`` prompt is sent.

Decision matrix exercised (privilege = bot admin):

    privileged,   no key,  require=True   -> OWNER      (anthropic_api_key = None)
    unprivileged, no key,  require=True   -> NeedsApiKey (refused, /apikey prompt)
    unprivileged, has key                 -> USER_KEY   (key injected)
    privileged,   has key                 -> USER_KEY   (key wins over owner)
    unprivileged, no key,  require=False  -> OWNER      (fallback, no key)
    no store (pre-SP2 dormant)            -> OWNER      (gate dormant, no key)
"""
from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bot.handlers import messages
from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.claude.session import TopicSession

USER_KEY = "sk-ant-api03-USERkey1234"

# Router attributes we mutate on the shared module-level router; snapshot and
# restore them so tests stay isolated from each other and from other bot tests.
_ROUTER_ATTRS = (
    "settings",
    "session_manager",
    "claude_bridge",
    "streamer",
    "bot",
    "event_bus",
    "connections_store",
    "api_key_store",
)


class _FakeBridge:
    """Records send_message kwargs and yields TEXT + COMPLETE events."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        yield ClaudeEvent(ClaudeEventType.TEXT, "Hi")
        yield ClaudeEvent(
            ClaudeEventType.COMPLETE,
            metadata={"session_id": "sdk-session", "usage": {"input_tokens": 1, "output_tokens": 1}},
        )

    def clear_session_cache(self, topic_id: int) -> None:  # pragma: no cover - defensive
        pass


class _FakeStore:
    """Minimal ApiKeyStore stand-in: returns a fixed key for any user."""

    def __init__(self, key: str | None) -> None:
        self._key = key
        self.lookups: list[int] = []

    def get_key(self, user_id: int) -> str | None:
        self.lookups.append(user_id)
        return self._key


class _FakeStreamer:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            tool_header_lines=[],
            response_buffer="",
            last_log_update=0,
        )
        self.streamed: list[str] = []
        self.finalize_calls: list[dict] = []

    async def create_log_message(self, *, chat_id: int, topic_id: int):
        return self.state

    async def update_log(self, state, content: str) -> None:
        return None

    async def stream_response(self, state, content: str) -> None:
        self.streamed.append(content)
        state.response_buffer += content

    async def finalize(self, state, **kwargs) -> None:
        self.finalize_calls.append(kwargs)


def _make_session_manager(*, is_admin: int | None) -> SimpleNamespace:
    """SessionManager stand-in. ``get_user_by_id`` drives is_bot_admin."""
    session = _SESSION

    def get_user_by_id(uid: int):
        if is_admin is None:
            return None
        return {"id": uid, "is_admin": is_admin}

    return SimpleNamespace(
        async_get_session=AsyncMock(return_value=session),
        async_mark_renamed=AsyncMock(),
        async_set_status=AsyncMock(),
        async_update_session_id=AsyncMock(),
        async_add_usage=AsyncMock(),
        async_get_usage=AsyncMock(return_value={}),
        async_clear_session_id=AsyncMock(),
        get_user_by_id=get_user_by_id,
    )


# Populated per-run inside _run so project_path points at a real temp dir.
_SESSION: TopicSession | None = None


def _make_message() -> SimpleNamespace:
    return SimpleNamespace(
        text="Hello Claude",
        chat=SimpleNamespace(id=555),
        message_thread_id=91,
        from_user=SimpleNamespace(username="tester", id=1166057082),
        answer=AsyncMock(),
    )


async def _run(
    *,
    is_admin: int | None,
    store_key: str | None,
    require_user_key: bool,
    with_store: bool = True,
    allowed_ids: tuple[int, ...] = (),
) -> tuple[_FakeBridge, SimpleNamespace, _FakeStreamer]:
    """Configure the shared router and drive one message through the handler."""
    global _SESSION
    saved = {a: getattr(messages.router, a, _MISSING) for a in _ROUTER_ATTRS}

    async def _noop(*args, **kwargs):
        return None

    with tempfile.TemporaryDirectory() as project_dir:
        _SESSION = TopicSession(
            topic_id=91,
            session_id="existing-session",
            project_path=project_dir,
            project_name="demo",
            is_renamed=True,  # skip the background rename task
        )
        bridge = _FakeBridge()
        streamer = _FakeStreamer()
        store = _FakeStore(store_key) if with_store else None

        messages.router.settings = SimpleNamespace(
            display=SimpleNamespace(
                show_logs=False,
                show_token_usage=False,
                show_context_usage=False,
                keep_log_after_response=False,
            ),
            require_user_key=require_user_key,
            get_light_project_paths=lambda: [project_dir],
            get_allowed_user_ids=lambda: list(allowed_ids),
        )
        messages.router.session_manager = _make_session_manager(is_admin=is_admin)
        messages.router.claude_bridge = bridge
        messages.router.streamer = streamer
        messages.router.bot = SimpleNamespace(send_chat_action=AsyncMock())
        messages.router.event_bus = None  # direct-bridge path
        messages.router.connections_store = None
        messages.router.api_key_store = store

        message = _make_message()

        try:
            with patch("src.bot.handlers.messages._typing_loop", new=_noop), patch(
                "src.bot.handlers.messages._heartbeat_loop", new=_noop
            ), patch(
                "src.bot.handlers.messages.refresh_commands_if_needed", new=_noop
            ):
                await messages.process_incoming_text(message, message.text)
        finally:
            for attr, value in saved.items():
                if value is _MISSING:
                    if hasattr(messages.router, attr):
                        delattr(messages.router, attr)
                else:
                    setattr(messages.router, attr, value)

    return bridge, message, streamer


_MISSING = object()


# --------------------------------------------------------------------------- #
# OWNER branch: privileged caller (bot admin) with no stored key.
# --------------------------------------------------------------------------- #
async def test_privileged_without_key_uses_owner_mode() -> None:
    bridge, message, _ = await _run(is_admin=1, store_key=None, require_user_key=True)

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    # No refusal prompt.
    message.answer.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Owner-lockout regression: the whitelisted owner (is_admin=0 after Telegram
# login) must NOT be refused / sent to /apikey — they ride owner credentials
# via ALLOWED_USER_IDS. Message user id (1166057082) is in the whitelist here.
# --------------------------------------------------------------------------- #
async def test_whitelisted_owner_without_key_uses_owner_mode() -> None:
    bridge, message, _ = await _run(
        is_admin=0,
        store_key=None,
        require_user_key=True,
        allowed_ids=(1166057082,),
    )

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    # Owner is not locked out: no /apikey prompt.
    message.answer.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Refusal branch: unprivileged caller, no key, require_user_key=True.
# --------------------------------------------------------------------------- #
async def test_unprivileged_without_key_required_is_refused() -> None:
    bridge, message, _ = await _run(is_admin=0, store_key=None, require_user_key=True)

    # Claude was never spawned.
    assert bridge.calls == []
    # The /apikey prompt was sent.
    message.answer.assert_awaited_once()
    (sent_text,) = message.answer.await_args.args
    assert "/apikey" in sent_text


# --------------------------------------------------------------------------- #
# USER_KEY branch: a stored key is injected regardless of privilege.
# --------------------------------------------------------------------------- #
async def test_unprivileged_with_key_injects_user_key() -> None:
    bridge, message, _ = await _run(is_admin=0, store_key=USER_KEY, require_user_key=True)

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] == USER_KEY
    message.answer.assert_not_awaited()


async def test_privileged_with_key_prefers_user_key() -> None:
    bridge, message, _ = await _run(is_admin=1, store_key=USER_KEY, require_user_key=True)

    # User key wins even for a privileged caller ("everyone pays with own key").
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] == USER_KEY


# --------------------------------------------------------------------------- #
# Fallback branch: unprivileged, no key, require_user_key=False → OWNER.
# --------------------------------------------------------------------------- #
async def test_unprivileged_without_key_not_required_falls_back_to_owner() -> None:
    bridge, message, _ = await _run(is_admin=0, store_key=None, require_user_key=False)

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    message.answer.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Backward-compat: no store (CONNECTIONS_SECRET_KEY unset) → gate dormant.
# --------------------------------------------------------------------------- #
async def test_no_store_keeps_owner_mode_dormant() -> None:
    # Even unprivileged + require_user_key=True must run: the gate is dormant
    # when the key store is absent (graceful degradation, owner creds).
    bridge, message, _ = await _run(
        is_admin=0, store_key=None, require_user_key=True, with_store=False
    )

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    message.answer.assert_not_awaited()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
