"""Unit tests for the per-user API key store and its Fernet crypto helper.

Covers ``ApiKeyCrypto`` (encrypt/decrypt round-trips and error handling) and
``ApiKeyStore`` (persistence, encryption-at-rest, metadata, upsert, deletion).
Each test gets an isolated temporary DB via the pytest ``tmp_path`` fixture.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

from cryptography.fernet import Fernet

from src.apikeys.crypto import ApiKeyCrypto
from src.apikeys.store import ApiKeyStore

SAMPLE_KEY = "sk-ant-api03-EXAMPLEplaintext1234"


def _crypto() -> ApiKeyCrypto:
    return ApiKeyCrypto(Fernet.generate_key().decode())


def _store(tmp_path, crypto: ApiKeyCrypto | None = None) -> ApiKeyStore:
    return ApiKeyStore(tmp_path / "apikeys.db", crypto or _crypto())


# --------------------------------------------------------------------------- #
# ApiKeyCrypto
# --------------------------------------------------------------------------- #
def test_crypto_roundtrip():
    crypto = _crypto()
    token = crypto.encrypt(SAMPLE_KEY)
    assert crypto.decrypt(token) == SAMPLE_KEY


def test_crypto_ciphertext_is_not_plaintext():
    crypto = _crypto()
    token = crypto.encrypt(SAMPLE_KEY)
    assert SAMPLE_KEY not in token
    assert token != SAMPLE_KEY


def test_crypto_decrypt_garbage_returns_none():
    crypto = _crypto()
    assert crypto.decrypt("not-a-valid-fernet-token") is None


def test_crypto_decrypt_wrong_key_returns_none():
    token = _crypto().encrypt(SAMPLE_KEY)
    # A different Fernet key cannot decrypt another key's ciphertext.
    assert _crypto().decrypt(token) is None


# --------------------------------------------------------------------------- #
# ApiKeyStore: set / get
# --------------------------------------------------------------------------- #
def test_set_and_get_key(tmp_path):
    store = _store(tmp_path)
    store.set_key(1, SAMPLE_KEY)
    assert store.get_key(1) == SAMPLE_KEY


def test_get_key_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get_key(999) is None


def test_plaintext_key_never_in_database(tmp_path):
    """The plaintext key must not appear anywhere in the DB file or columns."""
    db_path = tmp_path / "apikeys.db"
    store = ApiKeyStore(db_path, _crypto())
    store.set_key(7, SAMPLE_KEY)
    store.close()

    # 1) Raw file bytes must not contain the plaintext.
    assert SAMPLE_KEY.encode() not in db_path.read_bytes()

    # 2) The stored column must be ciphertext, not the plaintext.
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT key_encrypted, last4 FROM user_api_keys WHERE user_id = ?", (7,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] != SAMPLE_KEY
    assert SAMPLE_KEY not in row[0]
    # last4 is intentionally stored in the clear for UI display.
    assert row[1] == SAMPLE_KEY[-4:]


def test_isolation_between_users(tmp_path):
    store = _store(tmp_path)
    store.set_key(1, "sk-ant-user-one-AAAA")
    store.set_key(2, "sk-ant-user-two-BBBB")
    assert store.get_key(1) == "sk-ant-user-one-AAAA"
    assert store.get_key(2) == "sk-ant-user-two-BBBB"
    assert store.get_key(3) is None


def test_get_key_undecryptable_returns_none(tmp_path):
    """A key encrypted under a rotated master key decrypts to None, not an error."""
    crypto_a = _crypto()
    store_a = ApiKeyStore(tmp_path / "apikeys.db", crypto_a)
    store_a.set_key(5, SAMPLE_KEY)
    store_a.close()

    # New store with a different master key over the same DB (simulates rotation).
    store_b = ApiKeyStore(tmp_path / "apikeys.db", _crypto())
    assert store_b.get_key(5) is None
    # Metadata is still readable without decryption.
    assert store_b.has_key(5) is True


# --------------------------------------------------------------------------- #
# ApiKeyStore: has_key
# --------------------------------------------------------------------------- #
def test_has_key(tmp_path):
    store = _store(tmp_path)
    assert store.has_key(1) is False
    store.set_key(1, SAMPLE_KEY)
    assert store.has_key(1) is True
    assert store.has_key(2) is False


# --------------------------------------------------------------------------- #
# ApiKeyStore: get_meta
# --------------------------------------------------------------------------- #
def test_get_meta_returns_fields_without_decryption(tmp_path):
    store = _store(tmp_path)
    store.set_key(1, SAMPLE_KEY)
    meta = store.get_meta(1)
    assert meta is not None
    assert set(meta) == {"status", "last4", "created_at", "updated_at"}
    assert meta["status"] == "active"
    assert meta["last4"] == SAMPLE_KEY[-4:]
    assert meta["created_at"]
    assert meta["updated_at"]
    # No decrypted secret leaks through the metadata.
    assert SAMPLE_KEY not in str(meta)


def test_get_meta_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get_meta(404) is None


def test_get_meta_undecryptable_reports_needs_reentry(tmp_path):
    """A stored-but-undecryptable key (after key rotation) surfaces a distinct status."""
    crypto_a = _crypto()
    store_a = ApiKeyStore(tmp_path / "apikeys.db", crypto_a)
    store_a.set_key(5, SAMPLE_KEY)
    store_a.close()

    # New store with a different master key over the same DB (simulates rotation).
    store_b = ApiKeyStore(tmp_path / "apikeys.db", _crypto())
    meta = store_b.get_meta(5)
    assert meta is not None
    # Status is overridden from 'active' to a distinct re-entry signal.
    assert meta["status"] == "needs_reentry"
    # last4 and timestamps are preserved.
    assert meta["last4"] == SAMPLE_KEY[-4:]
    assert meta["created_at"]
    assert meta["updated_at"]
    # The (undecryptable) secret never leaks through the metadata.
    assert SAMPLE_KEY not in str(meta)


def test_set_key_custom_status(tmp_path):
    store = _store(tmp_path)
    store.set_key(1, SAMPLE_KEY, status="invalid")
    meta = store.get_meta(1)
    assert meta is not None
    assert meta["status"] == "invalid"


# --------------------------------------------------------------------------- #
# ApiKeyStore: upsert semantics
# --------------------------------------------------------------------------- #
def test_set_key_upsert_updates_key_and_timestamp(tmp_path):
    store = _store(tmp_path)
    with patch("src.apikeys.store.datetime") as mock_dt:
        mock_dt.now.return_value.isoformat.return_value = "2026-01-01T00:00:00"
        store.set_key(1, "sk-ant-old-XXXX")
    created = store.get_meta(1)["created_at"]

    with patch("src.apikeys.store.datetime") as mock_dt:
        mock_dt.now.return_value.isoformat.return_value = "2026-02-02T00:00:00"
        store.set_key(1, "sk-ant-new-YYYY", status="active")

    meta = store.get_meta(1)
    assert store.get_key(1) == "sk-ant-new-YYYY"
    assert meta["last4"] == "YYYY"
    # created_at is preserved across upserts; updated_at advances.
    assert meta["created_at"] == created == "2026-01-01T00:00:00"
    assert meta["updated_at"] == "2026-02-02T00:00:00"


# --------------------------------------------------------------------------- #
# ApiKeyStore: delete
# --------------------------------------------------------------------------- #
def test_delete_key(tmp_path):
    store = _store(tmp_path)
    store.set_key(1, SAMPLE_KEY)
    assert store.delete_key(1) is True
    assert store.get_key(1) is None
    assert store.has_key(1) is False
    assert store.get_meta(1) is None
    # Deleting a non-existent row returns False.
    assert store.delete_key(1) is False


def test_delete_missing_returns_false(tmp_path):
    store = _store(tmp_path)
    assert store.delete_key(123) is False


# --------------------------------------------------------------------------- #
# ApiKeyStore: connection reuse / close
# --------------------------------------------------------------------------- #
def test_persists_across_reopen(tmp_path):
    db_path = tmp_path / "apikeys.db"
    crypto = _crypto()
    store = ApiKeyStore(db_path, crypto)
    store.set_key(1, SAMPLE_KEY)
    store.close()

    reopened = ApiKeyStore(db_path, crypto)
    assert reopened.get_key(1) == SAMPLE_KEY
