"""Unit tests for the session authentication policy.

Covers :func:`resolve_session_auth`, the pure fail-closed decision function that
picks between a caller's own Anthropic API key (``USER_KEY``) and the owner's
subscription/credentials (``OWNER``). No I/O is involved, so every branch is
exercised directly.

Decision matrix (``is_privileged`` × ``user_key`` × ``require_user_key``):

    user_key present            -> USER_KEY   (always wins, any other input)
    no key, privileged          -> OWNER      (any require_user_key)
    no key, unprivileged, req    -> NeedsApiKeyError
    no key, unprivileged, !req   -> OWNER      (fallback mode)
"""
from __future__ import annotations

import dataclasses

import pytest

from src.apikeys.policy import (
    AuthDecision,
    AuthMode,
    NeedsApiKeyError,
    resolve_session_auth,
)

USER_KEY = "sk-ant-api03-USERkey1234"


# --------------------------------------------------------------------------- #
# USER_KEY branch: a supplied key always wins, regardless of other inputs
# --------------------------------------------------------------------------- #
def test_user_key_privileged_returns_user_key():
    decision = resolve_session_auth(
        is_privileged=True, user_key=USER_KEY, require_user_key=False
    )
    assert decision == AuthDecision(AuthMode.USER_KEY, USER_KEY)


def test_user_key_unprivileged_returns_user_key():
    decision = resolve_session_auth(
        is_privileged=False, user_key=USER_KEY, require_user_key=True
    )
    assert decision.mode is AuthMode.USER_KEY
    assert decision.api_key == USER_KEY


@pytest.mark.parametrize("is_privileged", [True, False])
@pytest.mark.parametrize("require_user_key", [True, False])
def test_user_key_takes_priority_over_owner(is_privileged, require_user_key):
    """Presence of a user key always wins over the OWNER path, in every combo."""
    decision = resolve_session_auth(
        is_privileged=is_privileged,
        user_key=USER_KEY,
        require_user_key=require_user_key,
    )
    assert decision.mode is AuthMode.USER_KEY
    assert decision.api_key == USER_KEY


# --------------------------------------------------------------------------- #
# OWNER branch: privileged caller with no key uses owner credentials
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("require_user_key", [True, False])
def test_privileged_without_key_returns_owner(require_user_key):
    decision = resolve_session_auth(
        is_privileged=True, user_key=None, require_user_key=require_user_key
    )
    assert decision == AuthDecision(AuthMode.OWNER, None)
    assert decision.api_key is None


# --------------------------------------------------------------------------- #
# Fallback branch: unprivileged, no key, key not required -> OWNER
# --------------------------------------------------------------------------- #
def test_unprivileged_without_key_fallback_returns_owner():
    decision = resolve_session_auth(
        is_privileged=False, user_key=None, require_user_key=False
    )
    assert decision == AuthDecision(AuthMode.OWNER, None)


# --------------------------------------------------------------------------- #
# Fail-closed branch: unprivileged, no key, key required -> refuse
# --------------------------------------------------------------------------- #
def test_unprivileged_without_key_required_raises():
    with pytest.raises(NeedsApiKeyError):
        resolve_session_auth(
            is_privileged=False, user_key=None, require_user_key=True
        )


def test_needs_api_key_error_has_message():
    with pytest.raises(NeedsApiKeyError, match="own API key"):
        resolve_session_auth(
            is_privileged=False, user_key=None, require_user_key=True
        )


def test_needs_api_key_error_is_exception():
    assert issubclass(NeedsApiKeyError, Exception)


# --------------------------------------------------------------------------- #
# Empty / whitespace keys are treated as "no key" (fail-closed)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_user_key_treated_as_absent_raises(blank):
    """An empty or whitespace-only key is not a real key -> unprivileged refuses."""
    with pytest.raises(NeedsApiKeyError):
        resolve_session_auth(
            is_privileged=False, user_key=blank, require_user_key=True
        )


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_user_key_privileged_falls_through_to_owner(blank):
    decision = resolve_session_auth(
        is_privileged=True, user_key=blank, require_user_key=True
    )
    assert decision == AuthDecision(AuthMode.OWNER, None)


# --------------------------------------------------------------------------- #
# AuthDecision / AuthMode shape
# --------------------------------------------------------------------------- #
def test_auth_mode_values():
    assert AuthMode.USER_KEY.value == "user_key"
    assert AuthMode.OWNER.value == "owner"
    assert {m.name for m in AuthMode} == {"USER_KEY", "OWNER"}


def test_auth_decision_is_frozen():
    decision = AuthDecision(AuthMode.OWNER, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.mode = AuthMode.USER_KEY  # type: ignore[misc]


def test_auth_decision_api_key_defaults_to_none():
    decision = AuthDecision(AuthMode.OWNER)
    assert decision.api_key is None
