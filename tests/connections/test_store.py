from cryptography.fernet import Fernet
from src.connections.crypto import SecretBox
from src.connections.store import ConnectionsStore


def _store(tmp_path):
    box = SecretBox(Fernet.generate_key().decode())
    return ConnectionsStore(tmp_path / "conn.db", box)


def test_connect_list_and_get_secret(tmp_path):
    store = _store(tmp_path)
    store.connect(user_id=42, service_id="notion", secret="tok-notion")
    assert store.list_service_ids(42) == ["notion"]
    assert store.get_secret(42, "notion") == "tok-notion"


def test_secret_is_encrypted_at_rest(tmp_path):
    store = _store(tmp_path)
    store.connect(user_id=7, service_id="github", secret="ghp_plain")
    raw = (tmp_path / "conn.db").read_bytes()
    assert b"ghp_plain" not in raw


def test_isolation_between_users(tmp_path):
    store = _store(tmp_path)
    store.connect(user_id=1, service_id="notion", secret="a")
    store.connect(user_id=2, service_id="github", secret="b")
    assert store.list_service_ids(1) == ["notion"]
    assert store.get_secret(2, "notion") is None


def test_disconnect(tmp_path):
    store = _store(tmp_path)
    store.connect(user_id=1, service_id="notion", secret="a")
    assert store.disconnect(1, "notion") is True
    assert store.list_service_ids(1) == []
    assert store.disconnect(1, "notion") is False
