"""Unit tests for the API-key service bootstrap (``build_api_key_store``).

The service turns a ``Settings`` object into a ready-to-use
:class:`~src.apikeys.store.ApiKeyStore`, degrading gracefully to ``None`` when
no ``CONNECTIONS_SECRET_KEY`` is configured (production default) and raising on
a present-but-malformed key so misconfiguration is loud rather than silent.

The tests use lightweight stand-ins for ``Settings``: plain objects exposing
``connections_secret_key`` / ``db_path`` attributes, plus one exercising the
accessor-method interface of the real ``Settings`` object
(``get_connections_secret_key`` / ``get_session_database_path``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from src.apikeys.service import build_api_key_store
from src.apikeys.store import ApiKeyStore


def _valid_key() -> str:
    return Fernet.generate_key().decode()


# --------------------------------------------------------------------------- #
# build_api_key_store
# --------------------------------------------------------------------------- #
def test_build_api_key_store_with_secret(tmp_path):
    """A valid secret yields a working ``ApiKeyStore``."""
    settings = SimpleNamespace(
        connections_secret_key=_valid_key(),
        db_path=str(tmp_path / "sessions.db"),
    )

    store = build_api_key_store(settings)

    assert isinstance(store, ApiKeyStore)


def test_build_api_key_store_without_secret(tmp_path):
    """A missing (``None``) secret degrades gracefully to ``None``."""
    settings = SimpleNamespace(
        connections_secret_key=None,
        db_path=str(tmp_path / "sessions.db"),
    )

    assert build_api_key_store(settings) is None


def test_build_api_key_store_empty_secret_returns_none(tmp_path):
    """An empty secret (unset ``CONNECTIONS_SECRET_KEY`` in prod) also degrades."""
    settings = SimpleNamespace(
        connections_secret_key="",
        db_path=str(tmp_path / "sessions.db"),
    )

    assert build_api_key_store(settings) is None


def test_build_api_key_store_invalid_secret(tmp_path):
    """A present-but-malformed secret raises rather than silently disabling."""
    settings = SimpleNamespace(
        connections_secret_key="not-a-valid-fernet-key",
        db_path=str(tmp_path / "sessions.db"),
    )

    with pytest.raises(Exception):
        build_api_key_store(settings)


def test_build_api_key_store_roundtrip(tmp_path):
    """The store returned by the builder actually encrypts and retrieves keys."""
    settings = SimpleNamespace(
        connections_secret_key=_valid_key(),
        db_path=str(tmp_path / "sessions.db"),
    )

    store = build_api_key_store(settings)
    assert store is not None
    store.set_key(42, "sk-ant-api03-EXAMPLE1234")
    assert store.get_key(42) == "sk-ant-api03-EXAMPLE1234"


def test_build_api_key_store_uses_settings_accessors(tmp_path):
    """The builder also supports the real ``Settings`` accessor-method interface."""
    db_path = tmp_path / "sessions.db"
    key = _valid_key()
    settings = SimpleNamespace(
        get_connections_secret_key=lambda: key,
        get_session_database_path=lambda: db_path,
    )

    store = build_api_key_store(settings)

    assert isinstance(store, ApiKeyStore)


def test_build_api_key_store_accessor_empty_returns_none(tmp_path):
    """An accessor returning '' (real ``Settings`` default) degrades to ``None``."""
    settings = SimpleNamespace(
        get_connections_secret_key=lambda: "",
        get_session_database_path=lambda: tmp_path / "sessions.db",
    )

    assert build_api_key_store(settings) is None


def test_build_api_key_store_default_db_path(monkeypatch, tmp_path):
    """With no db path configured the builder defaults to ``sessions.db``."""
    monkeypatch.chdir(tmp_path)
    settings = SimpleNamespace(connections_secret_key=_valid_key())

    store = build_api_key_store(settings)

    assert isinstance(store, ApiKeyStore)
    assert (tmp_path / "sessions.db").exists()
