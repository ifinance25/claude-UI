"""Bootstrap for the per-user Anthropic API-key store.

:func:`build_api_key_store` turns a ``Settings`` object into a ready-to-use
:class:`ApiKeyStore`. It degrades gracefully to ``None`` when no
``CONNECTIONS_SECRET_KEY`` is configured — the production default is an empty
string, so the SP2 per-user-key feature stays dormant instead of crashing the
bot on startup. A present-but-malformed key, however, is a misconfiguration and
is allowed to raise so it is loud rather than silent.

The resolver supports two ``Settings`` shapes:

* the real :class:`src.config.settings.Settings`, which exposes accessor
  methods (``get_connections_secret_key`` / ``get_session_database_path``); and
* lightweight test doubles / future callers that expose plain
  ``connections_secret_key`` / ``db_path`` attributes.
"""
from __future__ import annotations

from pathlib import Path

from src.apikeys.crypto import ApiKeyCrypto
from src.apikeys.store import ApiKeyStore

_DEFAULT_DB_PATH = "sessions.db"


def _resolve_secret(settings) -> str | None:
    """Return the Fernet master key from ``settings`` (accessor or attribute)."""
    getter = getattr(settings, "get_connections_secret_key", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
        if value:
            return value
    return getattr(settings, "connections_secret_key", None)


def _resolve_db_path(settings) -> str | Path:
    """Return the SQLite path for the store (accessor or attribute, with default)."""
    getter = getattr(settings, "get_session_database_path", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = None
        if value:
            return value
    return getattr(settings, "db_path", None) or _DEFAULT_DB_PATH


def build_api_key_store(settings) -> ApiKeyStore | None:
    """Build an :class:`ApiKeyStore`, or ``None`` when no secret is configured.

    * No / empty secret → ``None`` (graceful degradation; feature stays off).
    * Present but malformed secret → the underlying :class:`ApiKeyCrypto`
      raises (surfaced to the caller — do not swallow misconfiguration).
    * Valid secret → an initialised store backed by the session database.
    """
    secret = _resolve_secret(settings)
    if not secret:
        return None

    crypto = ApiKeyCrypto(secret)
    db_path = _resolve_db_path(settings)
    return ApiKeyStore(db_path, crypto)
