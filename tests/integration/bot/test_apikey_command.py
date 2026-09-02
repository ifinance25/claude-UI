"""Integration tests for the /apikey FSM command (SP2 per-user key entry).

The handlers are driven directly (not through a live Dispatcher) with fake
Message / FSMContext / ApiKeyStore doubles — the same style as
``test_auth_resolution``. ``probe_key`` is patched so the live Anthropic call is
never made; ``validate_key_format`` runs for real against the module's regex.

Covered paths:

* ``/apikey`` starts the FSM (prompt sent, state set)
* the pasted-key message is deleted immediately (security)
* bad format → error, stays in FSM, nothing stored
* probe 401 (INVALID) → error, stays in FSM, nothing stored
* valid key (probe VALID) → stored ``active``, "verified", state cleared
* network-unknown probe → stored ``unverified``, warning, state cleared
* ``/apikey delete`` → key removed, confirmation
* ``/cancel`` → FSM cleared, "Cancelled"
* feature off (no store) → polite disabled message, no state change
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.apikeys.validate import ProbeResult
from src.bot.handlers import apikey
from src.bot.handlers.apikey import ApiKeyFSM, cmd_apikey, cmd_cancel, on_key

USER_ID = 1166057082
VALID_KEY = "sk-ant-api03-" + "a" * 45  # matches KEY_PATTERN (sk-ant- + 40+ chars)


class _FakeState:
    """Minimal FSMContext stand-in tracking the current state and data."""

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def clear(self) -> None:
        self.state = None
        self.data = {}

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)


class _FakeStore:
    """Records set_key / delete_key calls; no real crypto or sqlite."""

    def __init__(self) -> None:
        self.set_calls: list[tuple[int, str, str]] = []
        self.deleted: list[int] = []

    def set_key(self, user_id: int, key: str, *, status: str = "active") -> None:
        self.set_calls.append((user_id, key, status))

    def delete_key(self, user_id: int) -> bool:
        self.deleted.append(user_id)
        return True

    def get_key(self, user_id: int) -> str | None:  # pragma: no cover - defensive
        return None


def _make_message(text: str, *, user_id: int = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=555),
        message_thread_id=None,
        from_user=SimpleNamespace(id=user_id, username="tester"),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )


_MISSING = object()


@pytest.fixture()
def store():
    """Inject a fake store on the module router; restore afterwards."""
    saved = getattr(apikey.router, "api_key_store", _MISSING)
    fake = _FakeStore()
    apikey.router.api_key_store = fake
    try:
        yield fake
    finally:
        if saved is _MISSING:
            if hasattr(apikey.router, "api_key_store"):
                delattr(apikey.router, "api_key_store")
        else:
            apikey.router.api_key_store = saved


# --------------------------------------------------------------------------- #
# /apikey — start the FSM
# --------------------------------------------------------------------------- #
async def test_apikey_starts_fsm(store) -> None:
    message = _make_message("/apikey")
    state = _FakeState()

    await cmd_apikey(message, state)

    message.answer.assert_awaited_once()
    (prompt,) = message.answer.await_args.args
    assert "sk-ant-" in prompt
    assert "/cancel" in prompt
    assert state.state == ApiKeyFSM.waiting_for_key


# --------------------------------------------------------------------------- #
# The key message is deleted immediately (security)
# --------------------------------------------------------------------------- #
async def test_key_message_deleted_immediately(store) -> None:
    message = _make_message(VALID_KEY)
    state = _FakeState()
    state.state = ApiKeyFSM.waiting_for_key

    with patch.object(apikey, "probe_key", AsyncMock(return_value=ProbeResult.VALID)):
        await on_key(message, state)

    message.delete.assert_awaited_once()


# --------------------------------------------------------------------------- #
# delete() raises (bot lacks rights / >48h) → warn prominently, still store
# --------------------------------------------------------------------------- #
async def test_delete_failure_warns_but_still_saves(store) -> None:
    message = _make_message(VALID_KEY)
    # Simulate a forum group where the bot cannot delete the message.
    message.delete = AsyncMock(side_effect=Exception("not enough rights"))
    state = _FakeState()
    state.state = ApiKeyFSM.waiting_for_key

    with patch.object(apikey, "probe_key", AsyncMock(return_value=ProbeResult.VALID)):
        await on_key(message, state)

    message.delete.assert_awaited_once()
    # Key is still stored so the flow is not blocked.
    assert store.set_calls == [(USER_ID, VALID_KEY, "active")]
    # The user is warned prominently that the secret stayed in chat history.
    (msg,) = message.answer.await_args.args
    assert "⚠️" in msg
    assert "вручную" in msg  # remove it manually
    assert "rotate" in msg or "перевыпустите" in msg  # rotate/reissue the key
    assert "saved" in msg  # the save confirmation is still present
    assert state.state is None  # FSM cleared


# --------------------------------------------------------------------------- #
# Bad format → error, stays in FSM, nothing stored / probed
# --------------------------------------------------------------------------- #
async def test_bad_format_stays_in_fsm(store) -> None:
    message = _make_message("not-a-real-key")
    state = _FakeState()
    state.state = ApiKeyFSM.waiting_for_key

    probe = AsyncMock(return_value=ProbeResult.VALID)
    with patch.object(apikey, "probe_key", probe):
        await on_key(message, state)

    message.delete.assert_awaited_once()
    (err,) = message.answer.await_args.args
    assert "Invalid format" in err
    assert state.state == ApiKeyFSM.waiting_for_key  # still waiting
    assert store.set_calls == []
    probe.assert_not_awaited()  # format check short-circuits before the probe


# --------------------------------------------------------------------------- #
# Probe 401 (INVALID) → error, stays in FSM, nothing stored
# --------------------------------------------------------------------------- #
async def test_probe_401_stays_in_fsm(store) -> None:
    message = _make_message(VALID_KEY)
    state = _FakeState()
    state.state = ApiKeyFSM.waiting_for_key

    with patch.object(apikey, "probe_key", AsyncMock(return_value=ProbeResult.INVALID)):
        await on_key(message, state)

    (err,) = message.answer.await_args.args
    assert "401" in err
    assert state.state == ApiKeyFSM.waiting_for_key
    assert store.set_calls == []


# --------------------------------------------------------------------------- #
# Valid key (probe VALID) → stored active, "verified", state cleared
# --------------------------------------------------------------------------- #
async def test_valid_key_saved_active(store) -> None:
    message = _make_message(VALID_KEY)
    state = _FakeState()
    state.state = ApiKeyFSM.waiting_for_key

    with patch.object(apikey, "probe_key", AsyncMock(return_value=ProbeResult.VALID)):
        await on_key(message, state)

    assert store.set_calls == [(USER_ID, VALID_KEY, "active")]
    (msg,) = message.answer.await_args.args
    assert "saved" in msg and "verified" in msg
    assert state.state is None  # FSM cleared


# --------------------------------------------------------------------------- #
# Network-unknown probe → stored unverified, soft warning, state cleared
# --------------------------------------------------------------------------- #
async def test_unknown_probe_saved_unverified(store) -> None:
    message = _make_message(VALID_KEY)
    state = _FakeState()
    state.state = ApiKeyFSM.waiting_for_key

    with patch.object(apikey, "probe_key", AsyncMock(return_value=ProbeResult.UNKNOWN)):
        await on_key(message, state)

    assert store.set_calls == [(USER_ID, VALID_KEY, "unverified")]
    (msg,) = message.answer.await_args.args
    assert "unverified" in msg
    assert state.state is None


# --------------------------------------------------------------------------- #
# /apikey delete → key removed, confirmation, no FSM
# --------------------------------------------------------------------------- #
async def test_apikey_delete(store) -> None:
    message = _make_message("/apikey delete")
    state = _FakeState()

    await cmd_apikey(message, state)

    assert store.deleted == [USER_ID]
    (msg,) = message.answer.await_args.args
    assert msg == "✓ API key deleted"
    assert state.state is None  # delete does not enter the FSM


# --------------------------------------------------------------------------- #
# /cancel → FSM cleared, cancellation message
# --------------------------------------------------------------------------- #
async def test_cancel_exits_fsm(store) -> None:
    message = _make_message("/cancel")
    state = _FakeState()
    state.state = ApiKeyFSM.waiting_for_key

    await cmd_cancel(message, state)

    assert state.state is None
    (msg,) = message.answer.await_args.args
    assert msg == "Cancelled"


# --------------------------------------------------------------------------- #
# Feature off (no store) → polite disabled message, no state change
# --------------------------------------------------------------------------- #
async def test_apikey_disabled_without_store() -> None:
    saved = getattr(apikey.router, "api_key_store", _MISSING)
    apikey.router.api_key_store = None
    try:
        message = _make_message("/apikey")
        state = _FakeState()

        await cmd_apikey(message, state)

        message.answer.assert_awaited_once()
        (msg,) = message.answer.await_args.args
        assert "выключено" in msg
        assert state.state is None
    finally:
        if saved is _MISSING:
            if hasattr(apikey.router, "api_key_store"):
                delattr(apikey.router, "api_key_store")
        else:
            apikey.router.api_key_store = saved


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
