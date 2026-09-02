"""Regression test for the SP2 relay *wiring* in :mod:`src.bot.core`.

The SP2 auth gate lives in :class:`src.event_bus.claude.ClaudeEventRelay`, and
its logic is already covered directly in
:mod:`tests.integration.event_bus.test_claude_relay`. But that suite constructs
the relay *by hand*, passing ``api_key_store`` and ``settings`` explicitly — so
it would keep passing even if :class:`src.bot.core.TelegramClaudeBot` forgot to
thread those collaborators into the relay. That omission was the original SP2
blocker: the gate silently stayed dormant in production and every unprivileged
caller borrowed the owner's credentials.

This test closes that gap. It instantiates the **real** ``TelegramClaudeBot``
exactly as ``main`` does (only network/heavy collaborators are stubbed) so the
relay is wired *by core.py itself*, backed by a **real** :class:`ApiKeyStore`
(built through ``build_api_key_store`` from a live ``CONNECTIONS_SECRET_KEY``)
and the real ``Settings`` whose ``require_user_key`` resolves ``True``. It then
drives a ``UserMessageReceived`` through the bot's own event bus and asserts:

* unprivileged sender, **no** stored key -> the session is **refused**
  (``AgentFinished.error == NEEDS_API_KEY_MESSAGE``; the bridge is never
  spawned — i.e. NOT an owner fallback); and
* unprivileged sender **with** a stored key -> the relay injects that key into
  ``bridge.send_message(anthropic_api_key=<key>)``.

If ``core.py``'s relay construction were reverted to drop ``api_key_store=`` /
``settings=``, the gate goes dormant: the refusal case would spawn the bridge
under owner creds, and the injection case would pass ``anthropic_api_key=None``.
Both assertions below then fail — which is the point.
"""
from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from src.bot.core import TelegramClaudeBot
from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.config.settings import Settings
from src.event_bus.claude import NEEDS_API_KEY_MESSAGE
from src.event_bus.events import AgentFinished, UserMessageReceived

USER_KEY = "sk-ant-api03-USERkey-realstore-1234"
UNPRIVILEGED_USER_ID = 4242


class _RecordingBridge:
    """Stand-in for the Claude bridge: records send_message kwargs, no spawn."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        yield ClaudeEvent(
            ClaudeEventType.COMPLETE,
            metadata={"session_id": "sdk-session", "usage": {"input_tokens": 1}},
        )


def _make_event(*, privileged: bool, user_id: int) -> UserMessageReceived:
    return UserMessageReceived(
        request_id="req-1",
        chat_id=111,
        user_id=user_id,
        topic_id=77,
        project_path="/tmp/demo-project",
        project_name="demo-project",
        text="hello",
        session_id="existing-session",
        privileged=privileged,
    )


def _collect_finished(bus) -> list[AgentFinished]:
    seen: list[AgentFinished] = []
    bus.subscribe(AgentFinished, lambda event: seen.append(event))
    return seen


@contextlib.contextmanager
def _wired_bot(tmp_path):
    """Build the real ``TelegramClaudeBot`` so the relay is wired by core.py.

    Only network / heavy collaborators are stubbed. Left REAL and un-patched:
    the ``EventBus``, the ``ClaudeEventRelay`` (the object under test) and
    ``build_api_key_store`` (yields a genuine SQLite-backed, Fernet-encrypting
    ``ApiKeyStore``). ``require_user_key`` resolves ``True`` from the default
    config, matching production Path B.
    """
    fernet_key = Fernet.generate_key().decode()
    db_path = tmp_path / "sessions.db"
    env = {
        "CONNECTIONS_SECRET_KEY": fernet_key,
        "SESSION_DATABASE_PATH": str(db_path),
    }
    with patch.dict(os.environ, env, clear=False):
        # An env override would defeat the config default we rely on — drop it
        # (patch.dict restores the previous value on exit).
        os.environ.pop("CLAUDE_REQUIRE_USER_KEY", None)
        settings = Settings(
            _env_file=None,  # isolate from any real .env on this machine
            telegram={"token": "123456:test-token"},
            claude={"transport": "sdk", "require_user_key": True},
            webhooks={"enabled": False},
        )
        # Sanity: the gate must be armed, or the refusal case is meaningless.
        assert settings.require_user_key is True

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch(
                    "src.bot.core.AiohttpSession",
                    return_value=SimpleNamespace(_proxy=None),
                )
            )
            for target in (
                "src.bot.core.Bot",
                "src.bot.core.ClaudeBridge",
                "src.bot.core.ResponseStreamer",
                "src.bot.core.WebhookAPIServer",
                "src.bot.core.CronScheduler",
                "src.bot.core.SessionManager",
                "src.web.server.WebServer",
                "src.web.message_store.MessageHistoryPersister",
            ):
                stack.enter_context(patch(target))
            # Connections aren't under test; keep the relay's connections path off.
            stack.enter_context(
                patch(
                    "src.connections.service.build_connections_store",
                    return_value=None,
                )
            )
            stack.enter_context(
                patch.object(TelegramClaudeBot, "_setup_middleware", lambda self: None)
            )
            stack.enter_context(
                patch.object(TelegramClaudeBot, "_setup_handlers", lambda self: None)
            )
            bot = TelegramClaudeBot(settings)
    try:
        yield bot
    finally:
        store = getattr(bot, "api_key_store", None)
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()


# --------------------------------------------------------------------------- #
# Refusal: core.py wired the store+settings, so an unprivileged caller with no
# key is refused fail-closed (NOT an owner fallback).
# --------------------------------------------------------------------------- #
async def test_core_wiring_refuses_unprivileged_without_key(tmp_path) -> None:
    with _wired_bot(tmp_path) as bot:
        # The relay must actually be carrying the collaborators core.py owns;
        # if the wiring were reverted these would be None and the gate dormant.
        assert bot.claude_event_relay.api_key_store is bot.api_key_store
        assert bot.api_key_store is not None  # real store (secret configured)
        assert bot.claude_event_relay.settings is bot.settings

        bridge = _RecordingBridge()
        bot.claude_event_relay.claude_bridge = bridge
        finished = _collect_finished(bot.event_bus)

        await bot.event_bus.publish(
            _make_event(privileged=False, user_id=UNPRIVILEGED_USER_ID)
        )

        # Refused fail-closed: Claude was never spawned (no owner fallback).
        assert bridge.calls == []
        assert finished, "expected an AgentFinished carrying the needs-key error"
        assert finished[-1].error == NEEDS_API_KEY_MESSAGE


# --------------------------------------------------------------------------- #
# Injection: an unprivileged caller WITH a stored key has it decrypted from the
# real store and injected into the bridge call.
# --------------------------------------------------------------------------- #
async def test_core_wiring_injects_stored_user_key(tmp_path) -> None:
    with _wired_bot(tmp_path) as bot:
        # Persist a key through the real (encrypting) store the bot created.
        bot.api_key_store.set_key(UNPRIVILEGED_USER_ID, USER_KEY)

        bridge = _RecordingBridge()
        bot.claude_event_relay.claude_bridge = bridge
        finished = _collect_finished(bot.event_bus)

        await bot.event_bus.publish(
            _make_event(privileged=False, user_id=UNPRIVILEGED_USER_ID)
        )

        assert len(bridge.calls) == 1
        assert bridge.calls[0]["anthropic_api_key"] == USER_KEY
        assert finished and finished[-1].error is None
