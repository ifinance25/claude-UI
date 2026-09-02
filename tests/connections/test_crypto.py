import base64
import pytest
from cryptography.fernet import Fernet
from src.connections import crypto
from src.connections.crypto import ConnectionsDisabledError, SecretDecryptError


def _key() -> str:
    return Fernet.generate_key().decode()


def test_roundtrip_encrypt_decrypt(monkeypatch):
    monkeypatch.setenv("CONNECTIONS_SECRET_KEY", _key())
    box = crypto.SecretBox.from_env()
    token = box.encrypt("s3cr3t-token")
    assert token != "s3cr3t-token"
    assert box.decrypt(token) == "s3cr3t-token"


def test_disabled_without_key(monkeypatch):
    monkeypatch.delenv("CONNECTIONS_SECRET_KEY", raising=False)
    assert crypto.is_enabled() is False
    with pytest.raises(ConnectionsDisabledError):
        crypto.SecretBox.from_env()


def test_enabled_with_key(monkeypatch):
    monkeypatch.setenv("CONNECTIONS_SECRET_KEY", _key())
    assert crypto.is_enabled() is True


def test_decrypt_wrong_key_raises():
    box_a = crypto.SecretBox(_key())
    box_b = crypto.SecretBox(_key())
    token = box_a.encrypt("s3cr3t-token")
    with pytest.raises(SecretDecryptError):
        box_b.decrypt(token)


def test_decrypt_malformed_input_raises():
    box = crypto.SecretBox(_key())
    with pytest.raises(SecretDecryptError):
        box.decrypt("not-base64!!")
