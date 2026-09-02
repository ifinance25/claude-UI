"""Integration tests for the SP2 auth gate in :class:`ClaudeEventRelay`.

The relay resolves *which credentials* a session runs against before it spawns
Claude (see :func:`src.apikeys.policy.resolve_session_auth`). These tests drive a
``UserMessageReceived`` through a real :class:`EventBus` with a fake bridge/store
and assert the resolved key that reaches ``bridge.send_message`` (or, for the
refusal path, that no run happens and an error is surfaced).

Decision matrix exercised:

    privileged,   no key                 -> OWNER      (anthropic_api_key = None)
    unprivileged, no key, require=True    -> NeedsApiKey (refused, no run)
    unprivileged, has key                 -> USER_KEY   (key injected)
    privileged,   has key                 -> USER_KEY   (key wins over owner)
    unprivileged, no key, require=False   -> OWNER      (fallback, no key)
    no store / no settings (pre-SP2)      -> OWNER      (gate dormant)
    no store, settings require=True       -> OWNER      (dormant: can't set key)
"""
from __future__ import annotations

from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.event_bus import ClaudeEventRelay, EventBus
from src.event_bus.claude import NEEDS_API_KEY_MESSAGE
from src.event_bus.events import AgentFinished, UserMessageReceived

USER_KEY = "sk-ant-api03-USERkey1234"


class _FakeBridge:
    """Records send_message kwargs and yields a single COMPLETE event."""

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


class _FakeSettings:
    def __init__(self, require_user_key: bool) -> None:
        self.require_user_key = require_user_key


def _make_event(*, privileged: bool, user_id: int = 42) -> UserMessageReceived:
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


def _collect_finished(bus: EventBus) -> list[AgentFinished]:
    seen: list[AgentFinished] = []
    bus.subscribe(AgentFinished, lambda event: seen.append(event))
    return seen


# --------------------------------------------------------------------------- #
# OWNER branch: privileged caller with no stored key uses owner credentials.
# --------------------------------------------------------------------------- #
async def test_privileged_without_key_uses_owner_mode() -> None:
    bus = EventBus()
    bridge = _FakeBridge()
    ClaudeEventRelay(
        bus=bus,
        claude_bridge=bridge,
        api_key_store=_FakeStore(None),
        settings=_FakeSettings(require_user_key=True),
    )
    finished = _collect_finished(bus)

    await bus.publish(_make_event(privileged=True))

    # Run proceeded and no key was injected (OWNER mode → owner credentials).
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    # Finished normally, no error surfaced.
    assert finished and finished[-1].error is None


# --------------------------------------------------------------------------- #
# Refusal branch: unprivileged caller, no key, require_user_key=True.
# --------------------------------------------------------------------------- #
async def test_unprivileged_without_key_required_is_refused() -> None:
    bus = EventBus()
    bridge = _FakeBridge()
    ClaudeEventRelay(
        bus=bus,
        claude_bridge=bridge,
        api_key_store=_FakeStore(None),
        settings=_FakeSettings(require_user_key=True),
    )
    finished = _collect_finished(bus)

    await bus.publish(_make_event(privileged=False))

    # Normal flow skipped: Claude was never spawned.
    assert bridge.calls == []
    # Error surfaced to the user via AgentFinished.
    assert finished, "expected an AgentFinished with the refusal message"
    assert finished[-1].error == NEEDS_API_KEY_MESSAGE


# --------------------------------------------------------------------------- #
# USER_KEY branch: a stored key is injected regardless of privilege.
# --------------------------------------------------------------------------- #
async def test_unprivileged_with_key_injects_user_key() -> None:
    bus = EventBus()
    bridge = _FakeBridge()
    store = _FakeStore(USER_KEY)
    ClaudeEventRelay(
        bus=bus,
        claude_bridge=bridge,
        api_key_store=store,
        settings=_FakeSettings(require_user_key=True),
    )
    finished = _collect_finished(bus)

    await bus.publish(_make_event(privileged=False, user_id=999))

    assert store.lookups == [999]  # looked up by the sender's user_id
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] == USER_KEY
    assert finished and finished[-1].error is None


async def test_privileged_with_key_prefers_user_key() -> None:
    bus = EventBus()
    bridge = _FakeBridge()
    ClaudeEventRelay(
        bus=bus,
        claude_bridge=bridge,
        api_key_store=_FakeStore(USER_KEY),
        settings=_FakeSettings(require_user_key=True),
    )

    await bus.publish(_make_event(privileged=True))

    # User key wins even for a privileged caller ("everyone pays with own key").
    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] == USER_KEY


# --------------------------------------------------------------------------- #
# Fallback branch: unprivileged, no key, require_user_key=False → OWNER.
# --------------------------------------------------------------------------- #
async def test_unprivileged_without_key_not_required_falls_back_to_owner() -> None:
    bus = EventBus()
    bridge = _FakeBridge()
    ClaudeEventRelay(
        bus=bus,
        claude_bridge=bridge,
        api_key_store=_FakeStore(None),
        settings=_FakeSettings(require_user_key=False),
    )
    finished = _collect_finished(bus)

    await bus.publish(_make_event(privileged=False))

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    assert finished and finished[-1].error is None


# --------------------------------------------------------------------------- #
# Dormant branch: no store (CONNECTIONS_SECRET_KEY unset) but require=True.
# The gate must stay dormant — an unprivileged no-key caller CANNOT set a key
# (/api/apikey returns 501 without the store), so refusing would be a dead-end.
# Instead the session must run on owner credentials.
# --------------------------------------------------------------------------- #
async def test_no_store_with_require_stays_dormant_uses_owner_mode() -> None:
    bus = EventBus()
    bridge = _FakeBridge()
    # No api_key_store, yet settings demand a user key. Without a store the
    # user has no way to supply one, so the gate must degrade to owner creds.
    ClaudeEventRelay(
        bus=bus,
        claude_bridge=bridge,
        api_key_store=None,
        settings=_FakeSettings(require_user_key=True),
    )
    finished = _collect_finished(bus)

    # Unprivileged caller with no key must still RUN, not be refused.
    await bus.publish(_make_event(privileged=False))

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    assert finished and finished[-1].error is None


# --------------------------------------------------------------------------- #
# Backward-compat: no store / no settings (pre-SP2 wiring) → gate dormant.
# --------------------------------------------------------------------------- #
async def test_no_store_no_settings_keeps_owner_mode() -> None:
    bus = EventBus()
    bridge = _FakeBridge()
    # Exactly the pre-SP2 construction — no api_key_store, no settings.
    ClaudeEventRelay(bus=bus, claude_bridge=bridge)
    finished = _collect_finished(bus)

    # Even an unprivileged caller must run (feature dormant, owner creds).
    await bus.publish(_make_event(privileged=False))

    assert len(bridge.calls) == 1
    assert bridge.calls[0]["anthropic_api_key"] is None
    assert finished and finished[-1].error is None
