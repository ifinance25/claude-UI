from cryptography.fernet import Fernet
from src.connections.service import build_connections_store
from src.connections.store import ConnectionsStore


class _Settings:
    def __init__(self, key, db):
        self._key = key
        self._db = db
    def get_connections_secret_key(self):
        return self._key
    def get_session_database_path(self):
        return self._db


def test_returns_store_when_key_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECTIONS_SECRET_KEY", Fernet.generate_key().decode())
    s = _Settings(Fernet.generate_key().decode(), tmp_path / "sessions.db")
    store = build_connections_store(s)
    assert isinstance(store, ConnectionsStore)


def test_returns_none_when_key_absent():
    s = _Settings("", "/tmp/x.db")
    assert build_connections_store(s) is None
