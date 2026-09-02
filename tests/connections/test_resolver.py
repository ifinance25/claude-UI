from cryptography.fernet import Fernet
from src.connections.crypto import SecretBox
from src.connections.store import ConnectionsStore
from src.connections.resolver import build_mcp_servers


def _store(tmp_path):
    return ConnectionsStore(tmp_path / "c.db", SecretBox(Fernet.generate_key().decode()))


def test_build_returns_server_with_injected_secret(tmp_path):
    store = _store(tmp_path)
    store.connect(user_id=1, service_id="notion", secret="tok-x")
    servers = build_mcp_servers(1, store)
    assert "notion" in servers
    assert servers["notion"]["env"]["NOTION_TOKEN"] == "tok-x"


def test_empty_for_user_without_connections(tmp_path):
    assert build_mcp_servers(999, _store(tmp_path)) == {}


def test_unknown_service_id_is_skipped(tmp_path):
    store = _store(tmp_path)
    store.connect(user_id=1, service_id="ghost", secret="x")  # not in catalog
    assert build_mcp_servers(1, store) == {}


def test_isolation_user_b_not_leaked(tmp_path):
    store = _store(tmp_path)
    store.connect(user_id=1, service_id="notion", secret="a")
    store.connect(user_id=2, service_id="github", secret="b")
    assert set(build_mcp_servers(1, store)) == {"notion"}
