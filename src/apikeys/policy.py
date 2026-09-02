"""Fail-closed policy for choosing session authentication.

A single pure function, :func:`resolve_session_auth`, decides whether a session
should run against the *caller's own* Anthropic API key (:attr:`AuthMode.USER_KEY`)
or fall back to the *owner's* subscription/credentials (:attr:`AuthMode.OWNER`).

Design notes:

* **No I/O, no side effects.** All inputs are passed in; callers (web via the
  event bus, bot via the messages handler) are responsible for looking up the
  user's key and privilege first.
* **User key always wins.** If a caller supplied their own key it is used
  regardless of privilege, so "everyone pays with their own key" holds.
* **Fail-closed.** When privilege is unclear it must be resolved to
  *unprivileged* by the caller; an unprivileged caller with no key and
  ``require_user_key=True`` is refused with :class:`NeedsApiKeyError` rather than
  silently borrowing the owner's credentials.
* Blank / whitespace-only keys are treated as "no key" so an empty form field
  never counts as a real key.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class AuthMode(enum.Enum):
    """Which credentials a session should authenticate with."""

    USER_KEY = "user_key"
    OWNER = "owner"


@dataclass(frozen=True)
class AuthDecision:
    """Immutable result of :func:`resolve_session_auth`.

    ``api_key`` is populated only for :attr:`AuthMode.USER_KEY`; it is ``None``
    for :attr:`AuthMode.OWNER`.
    """

    mode: AuthMode
    api_key: str | None = None


class NeedsApiKeyError(Exception):
    """Raised when a caller must supply their own API key but has not."""


def resolve_session_auth(
    *,
    is_privileged: bool,
    user_key: str | None,
    require_user_key: bool,
) -> AuthDecision:
    """Decide how a session authenticates.

    Args:
        is_privileged: True for the owner / admins who may use owner credentials.
            If privilege cannot be determined it must be passed as ``False``.
        user_key: The caller's own Anthropic API key, or ``None``. Blank or
            whitespace-only values are treated as ``None``.
        require_user_key: When True, unprivileged callers *must* provide their
            own key; otherwise they fall back to owner credentials.

    Returns:
        An :class:`AuthDecision`.

    Raises:
        NeedsApiKeyError: Unprivileged caller with no key while a key is required.
    """
    if user_key and user_key.strip():
        return AuthDecision(AuthMode.USER_KEY, user_key)
    if is_privileged:
        return AuthDecision(AuthMode.OWNER, None)
    if require_user_key:
        raise NeedsApiKeyError("User must provide their own API key")
    return AuthDecision(AuthMode.OWNER, None)
