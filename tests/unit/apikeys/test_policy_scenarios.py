"""Policy-scenario tests for the SP2 per-user-key decision matrix.

NOTE: This is a **pure-policy** test, not an integration test. ``drive_session``
below *re-implements* the tiny gate (look up the key, call
:func:`resolve_session_auth`, dispatch/refuse) rather than driving the real
``ClaudeEventRelay`` / ``process_incoming_text`` wiring — so it guards the
*decision matrix* against a real (Fernet + sqlite) key store, but it does NOT
guard the production wiring. The seam the original SP2 blocker slipped through —
routes_ws.py / core.py actually threading privilege + store + settings into the
relay — is covered separately by:

    * tests/integration/event_bus/test_claude_relay.py    (relay gate)
    * tests/integration/bot/test_sp2_wiring.py            (core.py wires the relay)
    * tests/integration/bot/test_sp2_production_path.py   (bot handler -> relay)
    * tests/integration/web/test_ws_sp2_gate.py           (routes_ws.py -> relay)

What *this* file pins down: given a real encrypting ``ApiKeyStore``,
:func:`resolve_session_auth` picks the right credentials for every
(privilege × stored key × require_user_key) combination. Nothing in the decision
path is mocked: the store encrypts/decrypts for real and
:func:`resolve_session_auth` runs unmodified. Only the Claude bridge's
``send_message`` is a spy, so we can assert *which credentials* actually reach
the run (or that it is never spawned for the refusal path).

Decision matrix covered (privilege × stored key × require_user_key):

    unprivileged, has key                 -> USER_KEY   (own key injected)
    unprivileged, no key,  require=True    -> NeedsApiKey (refused, no run)
    privileged,   no key                  -> OWNER      (subscription fallback)
    privileged,   has key                 -> USER_KEY   (own key wins over owner)
    unprivileged, no key,  require=False   -> OWNER      (gate dormant/transitional)
    stored blank/whitespace key           -> treated as no key (fail-closed)
    no store at all (pre-SP2 wiring)       -> OWNER      (gate dormant)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from src.apikeys.crypto import ApiKeyCrypto
from src.apikeys.policy import AuthMode, NeedsApiKeyError, resolve_session_auth
from src.apikeys.store import ApiKeyStore

USER_KEY = "sk-ant-api03-" + "u" * 40
OTHER_KEY = "sk-ant-api03-" + "o" * 40

# Non-admin caller (must bring their own key when the gate is enforced).
UNPRIVILEGED_ID = 999_000_111
# Admin / owner (may fall back to the service subscription).
PRIVILEGED_ID = 1_166_057_082


# --------------------------------------------------------------------------- #
# Fixtures: a real encrypted store and a spy for the Claude bridge.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def store(tmp_path):
    """A real :class:`ApiKeyStore` backed by Fernet crypto and a temp sqlite DB."""
    crypto = ApiKeyCrypto(Fernet.generate_key().decode())
    s = ApiKeyStore(tmp_path / "apikeys.db", crypto)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def bridge():
    """Stand-in Claude bridge that records ``send_message`` invocations."""
    return SimpleNamespace(send_message=AsyncMock())


# --------------------------------------------------------------------------- #
# The gate under test, reproduced exactly as bot/web wire it (no policy mocking).
# --------------------------------------------------------------------------- #
async def drive_session(
    *,
    store: ApiKeyStore | None,
    user_id: int,
    is_privileged: bool,
    require_user_key: bool,
    bridge,
):
    """Run the SP2 auth gate end-to-end for one message.

    Mirrors ``src.bot.handlers.messages`` / ``ClaudeEventRelay``: look the
    caller's key up in the store, resolve the auth decision, and — only when the
    caller is *not* refused — dispatch to ``bridge.send_message`` with the chosen
    credentials. When the store is absent the gate is dormant (owner creds), so
    ``require_user_key`` is forced off, matching production wiring.

    Returns the :class:`~src.apikeys.policy.AuthDecision`. Propagates
    :class:`NeedsApiKeyError` (having *never* called ``send_message``) when the
    caller must bring a key but has not.
    """
    user_key = store.get_key(user_id) if store is not None else None
    effective_require = require_user_key if store is not None else False

    decision = resolve_session_auth(
        is_privileged=is_privileged,
        user_key=user_key,
        require_user_key=effective_require,
    )

    # Reached only when the caller was NOT refused: spawn Claude with the
    # resolved credentials (None => owner subscription; a string => user key).
    await bridge.send_message(
        message="hello claude",
        topic_id=77,
        anthropic_api_key=decision.api_key,
    )
    return decision


def _sent_api_key(bridge):
    """The ``anthropic_api_key`` kwarg passed to the single send_message call."""
    return bridge.send_message.await_args.kwargs["anthropic_api_key"]


# --------------------------------------------------------------------------- #
# Scenario 1: non-privileged user with a per-user key -> USER_KEY mode.
# --------------------------------------------------------------------------- #
async def test_e2e_unprivileged_user_with_key_uses_user_key(store, bridge):
    store.set_key(UNPRIVILEGED_ID, USER_KEY)  # user configured their own key

    decision = await drive_session(
        store=store,
        user_id=UNPRIVILEGED_ID,
        is_privileged=False,
        require_user_key=True,
        bridge=bridge,
    )

    assert decision.mode is AuthMode.USER_KEY
    assert decision.api_key == USER_KEY
    # The message is processed under the caller's own API key.
    bridge.send_message.assert_awaited_once()
    assert _sent_api_key(bridge) == USER_KEY


# --------------------------------------------------------------------------- #
# Scenario 2: non-privileged user WITHOUT a key + require=True -> refused.
# --------------------------------------------------------------------------- #
async def test_e2e_unprivileged_user_without_key_required_is_refused(store, bridge):
    # No key stored for this user.
    with pytest.raises(NeedsApiKeyError):
        await drive_session(
            store=store,
            user_id=UNPRIVILEGED_ID,
            is_privileged=False,
            require_user_key=True,
            bridge=bridge,
        )

    # Claude is never spawned — the run is refused fail-closed.
    bridge.send_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Scenario 3: privileged (admin) without a key -> OWNER mode (fallback).
# --------------------------------------------------------------------------- #
async def test_e2e_privileged_user_without_key_falls_back_to_owner(store, bridge):
    decision = await drive_session(
        store=store,
        user_id=PRIVILEGED_ID,
        is_privileged=True,
        require_user_key=True,
        bridge=bridge,
    )

    assert decision.mode is AuthMode.OWNER
    assert decision.api_key is None
    # Processed under the service subscription (legacy behavior): no key injected.
    bridge.send_message.assert_awaited_once()
    assert _sent_api_key(bridge) is None


# --------------------------------------------------------------------------- #
# Scenario 4: privileged user WITH a key -> USER_KEY takes priority.
# --------------------------------------------------------------------------- #
async def test_e2e_privileged_user_with_key_prefers_user_key(store, bridge):
    store.set_key(PRIVILEGED_ID, USER_KEY)  # even an admin can bring their own

    decision = await drive_session(
        store=store,
        user_id=PRIVILEGED_ID,
        is_privileged=True,
        require_user_key=True,
        bridge=bridge,
    )

    # "Everyone pays with their own key" — the user key wins over owner creds.
    assert decision.mode is AuthMode.USER_KEY
    assert decision.api_key == USER_KEY
    bridge.send_message.assert_awaited_once()
    assert _sent_api_key(bridge) == USER_KEY


# --------------------------------------------------------------------------- #
# Scenario 5: non-privileged, no key, require=False -> OWNER (transitional).
# --------------------------------------------------------------------------- #
async def test_e2e_unprivileged_without_key_gate_disabled_falls_back(store, bridge):
    decision = await drive_session(
        store=store,
        user_id=UNPRIVILEGED_ID,
        is_privileged=False,
        require_user_key=False,  # SP2 gate not yet enforced
        bridge=bridge,
    )

    assert decision.mode is AuthMode.OWNER
    assert decision.api_key is None
    bridge.send_message.assert_awaited_once()
    assert _sent_api_key(bridge) is None


# --------------------------------------------------------------------------- #
# Fail-closed: a blank / whitespace stored key is treated as "no key".
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", ["   ", "\n\t"])
async def test_e2e_blank_stored_key_treated_as_absent(store, bridge, blank):
    # A corrupt/whitespace-only stored value must never count as a real key.
    store.set_key(UNPRIVILEGED_ID, blank)
    assert store.get_key(UNPRIVILEGED_ID) == blank  # round-trips through crypto

    with pytest.raises(NeedsApiKeyError):
        await drive_session(
            store=store,
            user_id=UNPRIVILEGED_ID,
            is_privileged=False,
            require_user_key=True,
            bridge=bridge,
        )

    bridge.send_message.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Backward-compat: no store at all (CONNECTIONS_SECRET_KEY unset) -> dormant.
# --------------------------------------------------------------------------- #
async def test_e2e_no_store_keeps_gate_dormant(bridge):
    # Even an unprivileged caller with require_user_key=True must run: the gate
    # is dormant when the key store is absent (graceful degradation, owner creds).
    decision = await drive_session(
        store=None,
        user_id=UNPRIVILEGED_ID,
        is_privileged=False,
        require_user_key=True,
        bridge=bridge,
    )

    assert decision.mode is AuthMode.OWNER
    assert decision.api_key is None
    bridge.send_message.assert_awaited_once()
    assert _sent_api_key(bridge) is None


# --------------------------------------------------------------------------- #
# Per-user isolation: the key is looked up by the *sender's* id, not shared.
# --------------------------------------------------------------------------- #
async def test_e2e_key_is_scoped_to_the_sending_user(store, bridge):
    # Only the privileged user configured a key; the unprivileged user did not.
    store.set_key(PRIVILEGED_ID, OTHER_KEY)

    # The unprivileged sender has no key of their own -> refused, does NOT borrow
    # another user's stored key.
    with pytest.raises(NeedsApiKeyError):
        await drive_session(
            store=store,
            user_id=UNPRIVILEGED_ID,
            is_privileged=False,
            require_user_key=True,
            bridge=bridge,
        )
    bridge.send_message.assert_not_awaited()

    # The owner of the key gets exactly their own key injected.
    decision = await drive_session(
        store=store,
        user_id=PRIVILEGED_ID,
        is_privileged=False,
        require_user_key=True,
        bridge=bridge,
    )
    assert decision.mode is AuthMode.USER_KEY
    assert _sent_api_key(bridge) == OTHER_KEY


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
