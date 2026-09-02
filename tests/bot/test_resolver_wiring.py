from cryptography.fernet import Fernet
from src.connections.crypto import SecretBox
from src.connections.store import ConnectionsStore
from src.bot.handlers.messages import mcp_servers_for
from src.event_bus.events import UserMessageReceived


def test_mcp_servers_for_none_store():
    assert mcp_servers_for(None, 1) is None


def test_mcp_servers_for_user(tmp_path):
    store = ConnectionsStore(tmp_path / "c.db", SecretBox(Fernet.generate_key().decode()))
    store.connect(user_id=1, service_id="notion", secret="tok")
    servers = mcp_servers_for(store, 1)
    assert servers and "notion" in servers


def test_mcp_servers_for_user_without_connections(tmp_path):
    store = ConnectionsStore(tmp_path / "c.db", SecretBox(Fernet.generate_key().decode()))
    assert mcp_servers_for(store, 999) is None


def _make_event(**overrides):
    fields = dict(
        request_id="r1",
        chat_id=-100,
        topic_id=7,
        project_path="/tmp/p",
        text="hi",
    )
    fields.update(overrides)
    return UserMessageReceived(**fields)


def test_user_message_received_carries_user_id():
    event = _make_event(user_id=5)
    assert event.user_id == 5


def test_user_message_received_user_id_defaults_to_zero():
    event = _make_event()
    assert event.user_id == 0
