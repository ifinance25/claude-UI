"""Production-path SP2 test: bot handler -> real EventBus -> real relay.

``tests/integration/bot/test_auth_resolution.py`` drives
``process_incoming_text`` through its *direct-bridge* branch (``event_bus`` unset)
— but production always runs the **event-bus** branch: the handler publishes a
``UserMessageReceived`` and a real :class:`~src.event_bus.claude.ClaudeEventRelay`
resolves the credentials and spawns Claude. That branch has two moving parts the
direct path never exercises:

* the handler must **propagate** the computed privilege onto the event
  (``UserMessageReceived(privileged=is_privileged)``) — the original SP2 blocker
  was privilege silently defaulting to ``False`` so an admin was *refused*; and
* the **relay** (not the handler) performs the fail-closed auth resolution and
  the ``anthropic_api_key`` injection.

This test wires a real ``EventBus`` + a real ``ClaudeEventRelay`` (backed by a
spy bridge and a stand-in key store), sets ``router.event_bus`` so
``process_incoming_text`` takes the production branch, and asserts the
credentials that actually reach the spy bridge *through the relay* — plus the
``privileged`` flag carried on the published event.

If the handler stopped threading ``privileged=is_privileged`` (regressing to the
default ``False``), the admin case below would be refused by the relay: the spy
bridge would never be called and ``AgentFinished.error`` would carry the
needs-key message. Both assertions then fail — which is the point.
"""
from __future__ import annotations

import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.bot.handlers import messages
from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.claude.session import TopicSession
from src.event_bus import ClaudeEventRelay, EventBus
from src.event_bus.claude import NEEDS_API_KEY_MESSAGE
from src.event_bus.events import AgentFinished, UserMessageReceived

USER_KEY = "sk-ant-api03-USERkey-production-path-1234"
# The sender id used by _make_message below.
SENDER_ID = 1166057082

_MISSING = object()
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


class _SpyBridge:
    """Relay's Claude bridge stand-in: records send_message kwargs, no spawn."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        yield ClaudeEvent(
            ClaudeEventType.COMPLETE,
            metadata={"session_id": "sdk-session", "usage": {"input_tokens": 1}},
        )


class _FakeStore:
    """Minimal ApiKeyStore stand-in: returns a fixed key for any user."""

    def __init__(self, key: str | None) -> None:
        self._key = key
        self.lookups: list[int] = []

    def get_key(self, user_id: int) -> str | None:
        self.lookups.append(user_id)
        return self._key


class _FakeSettingsForRelay:
    def __init__(self, require_user_key: bool) -> None:
        self.require_user_key = require_user_key


class _FakeStreamer:
    def __init__(self) -> None:
        self.state = SimpleNamespace(
            tool_header_lines=[], response_buffer="", last_log_update=0
        )
        self.finalize_calls: list[dict] = []

    async def create_log_message(self, *, chat_id: int, topic_id: int):
        return self.state

    async def update_log(self, state, content: str) -> None:
        return None

    async def stream_response(self, state, content: str) -> None:
        state.response_buffer += content

    async def finalize(self, state, **kwargs) -> None:
        self.finalize_calls.append(kwargs)


def _make_session_manager(*, is_admin: int | None, session: TopicSession) -> SimpleNamespace:
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


def _make_message() -> SimpleNamespace:
    return SimpleNamespace(
        text="Hello Claude",
        chat=SimpleNamespace(id=555),
        message_thread_id=91,
        from_user=SimpleNamespace(username="tester", id=SENDER_ID),
        answer=AsyncMock(),
    )


async def _run(
    *,
    is_admin: int | None,
    store_key: str | None,
    require_user_key: bool,
    allowed_ids: tuple[int, ...] = (),
) -> tuple[_SpyBridge, SimpleNamespace, list[UserMessageReceived], list[AgentFinished]]:
    """Drive one message through the PRODUCTION (event-bus) branch.

    Wires a real ``EventBus`` + ``ClaudeEventRelay`` (spy bridge + key store) and
    sets ``router.event_bus`` so ``process_incoming_text`` publishes rather than
    calling the bridge directly. Returns the spy bridge (relay's), the message,
    the captured ``UserMessageReceived`` events, and the ``AgentFinished`` events.
    """
    saved = {a: getattr(messages.router, a, _MISSING) for a in _ROUTER_ATTRS}

    async def _noop(*args, **kwargs):
        return None

    with tempfile.TemporaryDirectory() as project_dir:
        session = TopicSession(
            topic_id=91,
            session_id="existing-session",
            project_path=project_dir,
            project_name="demo",
            is_renamed=True,
        )
        bus = EventBus()
        spy = _SpyBridge()
        store = _FakeStore(store_key)
        relay = ClaudeEventRelay(
            bus=bus,
            claude_bridge=spy,
            api_key_store=store,
            settings=_FakeSettingsForRelay(require_user_key),
        )

        published: list[UserMessageReceived] = []
        finished: list[AgentFinished] = []
        bus.subscribe(UserMessageReceived, lambda e: published.append(e))
        bus.subscribe(AgentFinished, lambda e: finished.append(e))

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
        messages.router.session_manager = _make_session_manager(
            is_admin=is_admin, session=session
        )
        # Direct-branch bridge is NOT used on the event-bus path; the relay's spy
        # bridge is the real spawn point. Give the handler a harmless stand-in.
        messages.router.claude_bridge = SimpleNamespace(
            clear_session_cache=lambda topic_id: None
        )
        messages.router.streamer = _FakeStreamer()
        messages.router.bot = SimpleNamespace(send_chat_action=AsyncMock())
        messages.router.event_bus = bus  # PRODUCTION path
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
            relay.close()
            for attr, value in saved.items():
                if value is _MISSING:
                    if hasattr(messages.router, attr):
                        delattr(messages.router, attr)
                else:
                    setattr(messages.router, attr, value)

    return spy, message, published, finished


# --------------------------------------------------------------------------- #
# Admin, no key: privilege must be PROPAGATED so the relay runs OWNER mode and
# does NOT refuse. This is the original-blocker regression.
# --------------------------------------------------------------------------- #
async def test_admin_without_key_propagates_privilege_and_runs_owner() -> None:
    spy, message, published, finished = await _run(
        is_admin=1, store_key=None, require_user_key=True
    )

    # The published event carried privileged=True (not the default False).
    assert published and published[-1].privileged is True
    # The relay spawned Claude under owner creds (no key injected) — NOT refused.
    assert len(spy.calls) == 1
    assert spy.calls[0]["anthropic_api_key"] is None
    # No refusal surfaced and no /apikey prompt sent.
    assert finished and finished[-1].error is None
    message.answer.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Whitelisted owner (is_admin=0 after Telegram login) is privileged via
# ALLOWED_USER_IDS and must likewise NOT be refused.
# --------------------------------------------------------------------------- #
async def test_whitelisted_owner_without_key_runs_owner() -> None:
    spy, message, published, finished = await _run(
        is_admin=0,
        store_key=None,
        require_user_key=True,
        allowed_ids=(SENDER_ID,),
    )

    assert published and published[-1].privileged is True
    assert len(spy.calls) == 1
    assert spy.calls[0]["anthropic_api_key"] is None
    assert finished and finished[-1].error is None
    message.answer.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Unprivileged sender WITH a stored key: the relay injects that key.
# --------------------------------------------------------------------------- #
async def test_unprivileged_with_key_injected_via_relay() -> None:
    spy, message, published, finished = await _run(
        is_admin=0, store_key=USER_KEY, require_user_key=True
    )

    assert published and published[-1].privileged is False
    assert len(spy.calls) == 1
    assert spy.calls[0]["anthropic_api_key"] == USER_KEY
    assert finished and finished[-1].error is None
    message.answer.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Sanity floor: unprivileged, no key, require=True IS refused at the bot gate
# (the /apikey prompt) and the relay never spawns Claude. Confirms the gate is
# actually armed in this harness — so the passing admin case above is meaningful.
# --------------------------------------------------------------------------- #
async def test_unprivileged_without_key_is_refused() -> None:
    spy, message, published, finished = await _run(
        is_admin=0, store_key=None, require_user_key=True
    )

    # Refused before publish: no event, no spawn.
    assert published == []
    assert spy.calls == []
    message.answer.assert_awaited_once()
    (sent_text,) = message.answer.await_args.args
    assert "/apikey" in sent_text
    # The relay's refusal message is never surfaced here — the bot gate fired first.
    assert all(f.error != NEEDS_API_KEY_MESSAGE for f in finished)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
