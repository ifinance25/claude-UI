"""Tests for /api/projects and /api/sessions REST routes."""
from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.server import WebServer


def _sign(bot_token: str, payload: dict) -> dict:
    secret = hashlib.sha256(bot_token.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def setup(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    (tmp_dir / "demo").mkdir()

    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    server = WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
        project_paths=[tmp_dir / "demo"],
    )
    try:
        yield server
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def _login(client: AsyncClient) -> None:
    payload = _sign(
        "12345:abc",
        {"id": 100, "first_name": "A", "auth_date": int(time.time())},
    )
    resp = await client.post("/api/auth/telegram", json=payload)
    assert resp.status_code == 200


async def test_projects_list(setup):
    server = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "demo"


async def test_projects_requires_auth(setup):
    server = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/projects")
    assert resp.status_code == 401


async def test_sessions_crud(setup):
    server = setup
    project_path = str(server.project_paths[0])
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)

        # Create
        resp = await client.post(
            "/api/sessions",
            json={"project_path": project_path, "project_name": "demo"},
        )
        assert resp.status_code == 201
        body = resp.json()
        topic_id = body["topic_id"]
        session_uuid = body["session_uuid"]
        assert topic_id < 0  # Web sessions still get negative topic_ids
        assert len(session_uuid) == 32  # uuid4().hex

        # List
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        listed = resp.json()
        assert any(s["session_uuid"] == session_uuid for s in listed)
        # is_running surfaced in the listing; idle session defaults to False.
        match = next(s for s in listed if s["session_uuid"] == session_uuid)
        assert match["is_running"] is False

        # Delete (by UUID)
        resp = await client.delete(f"/api/sessions/{session_uuid}")
        assert resp.status_code == 204

        # Delete again -> 404
        resp = await client.delete(f"/api/sessions/{session_uuid}")
        assert resp.status_code == 404


async def test_sessions_create_uses_descending_negative_ids(setup):
    server = setup
    project_path = str(server.project_paths[0])
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        r1 = await client.post(
            "/api/sessions",
            json={"project_path": project_path, "project_name": "demo"},
        )
        r2 = await client.post(
            "/api/sessions",
            json={"project_path": project_path, "project_name": "demo"},
        )
    assert r1.json()["topic_id"] == -1
    assert r2.json()["topic_id"] == -2
    # UUIDs are also distinct
    assert r1.json()["session_uuid"] != r2.json()["session_uuid"]


async def test_messages_history(setup):
    server = setup
    sm = server.session_manager
    session = sm.create_session(
        topic_id=999,
        project_path=str(server.project_paths[0]),
        project_name="demo",
        chat_id=100,
    )
    conn = sm._get_connection()
    conn.execute(
        "INSERT INTO messages (topic_id, type, kind, content, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (999, "streaming_update", "text", "hello"),
    )
    conn.commit()

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get(f"/api/sessions/{session.session_uuid}/messages")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["content"] == "hello"


async def test_messages_history_since(setup):
    server = setup
    sm = server.session_manager
    session = sm.create_session(
        topic_id=999,
        project_path=str(server.project_paths[0]),
        project_name="demo",
        chat_id=100,
    )
    conn = sm._get_connection()
    for content in ["a", "b", "c"]:
        conn.execute(
            "INSERT INTO messages (topic_id, type, kind, content, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (999, "streaming_update", "text", content),
        )
    conn.commit()
    first_id = conn.execute(
        "SELECT MIN(event_id) FROM messages WHERE topic_id = ?", (999,)
    ).fetchone()[0]

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get(
            f"/api/sessions/{session.session_uuid}/messages?since={first_id}"
        )
    data = resp.json()
    assert [m["content"] for m in data] == ["b", "c"]


async def test_messages_history_unknown_uuid_returns_404(setup):
    server = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/sessions/nonexistent-uuid/messages")
    assert resp.status_code == 404


# --- Session search (?q=) ----------------------------------------------------


def _seed_session_with_last_message(
    server, *, topic_id: int, project_name: str, last_content: str, chat_id: int = 100
):
    """Create a session and a single message row to serve as 'last message'.

    Uses a unique path per session so projects.name doesn't get collapsed
    by the UNIQUE(abspath) constraint on the projects table.
    """
    sm = server.session_manager
    base_dir = Path(str(server.project_paths[0]))
    proj_dir = base_dir / f"{project_name}-{topic_id}"
    proj_dir.mkdir(parents=True, exist_ok=True)
    sm.create_session(
        topic_id=topic_id,
        project_path=str(proj_dir),
        project_name=project_name,
        chat_id=chat_id,
    )
    conn = sm._get_connection()
    conn.execute(
        "INSERT INTO messages (topic_id, type, kind, content, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (topic_id, "user_message", None, last_content),
    )
    conn.commit()


async def test_sessions_search_empty_q_returns_all(setup):
    server = setup
    _seed_session_with_last_message(
        server, topic_id=100, project_name="alpha", last_content="hello"
    )
    _seed_session_with_last_message(
        server, topic_id=101, project_name="beta", last_content="world"
    )

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/sessions")  # no q
    assert resp.status_code == 200
    topic_ids = {s["topic_id"] for s in resp.json()}
    assert {100, 101}.issubset(topic_ids)


async def test_sessions_search_matches_project_name(setup):
    server = setup
    _seed_session_with_last_message(
        server, topic_id=200, project_name="payments-service", last_content="lorem"
    )
    _seed_session_with_last_message(
        server, topic_id=201, project_name="billing-cron", last_content="ipsum"
    )

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/sessions?q=payments")
    assert resp.status_code == 200
    topic_ids = {s["topic_id"] for s in resp.json()}
    assert 200 in topic_ids
    assert 201 not in topic_ids


async def test_sessions_search_matches_last_message_content(setup):
    server = setup
    _seed_session_with_last_message(
        server, topic_id=300, project_name="proj-a", last_content="fix the auth bug"
    )
    _seed_session_with_last_message(
        server, topic_id=301, project_name="proj-b", last_content="refactor tests"
    )

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/sessions?q=AUTH")  # case-insensitive
    assert resp.status_code == 200
    topic_ids = {s["topic_id"] for s in resp.json()}
    assert 300 in topic_ids
    assert 301 not in topic_ids


async def test_sessions_search_no_matches_returns_empty(setup):
    server = setup
    _seed_session_with_last_message(
        server, topic_id=400, project_name="proj-a", last_content="lorem ipsum"
    )

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/sessions?q=this-substring-matches-nothing")
    assert resp.status_code == 200
    assert resp.json() == []


# --- Notes (PATCH /api/sessions/{uuid}) -------------------------------------


async def test_session_notes_default_null(setup):
    """Freshly-created sessions have no notes."""
    server = setup
    project_path = str(server.project_paths[0])
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.post(
            "/api/sessions",
            json={"project_path": project_path, "project_name": "demo"},
        )
        assert resp.status_code == 201
        assert resp.json()["notes"] is None


async def test_session_notes_patch_roundtrip(setup):
    server = setup
    project_path = str(server.project_paths[0])
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        create = await client.post(
            "/api/sessions",
            json={"project_path": project_path, "project_name": "demo"},
        )
        uuid_ = create.json()["session_uuid"]

        patched = await client.patch(
            f"/api/sessions/{uuid_}",
            json={"notes": "TODO: refactor auth flow"},
        )
        assert patched.status_code == 200
        assert patched.json()["notes"] == "TODO: refactor auth flow"

        # GET sessions confirms persistence
        listed = await client.get("/api/sessions")
        match = next(s for s in listed.json() if s["session_uuid"] == uuid_)
        assert match["notes"] == "TODO: refactor auth flow"


async def test_session_notes_patch_unknown_uuid_returns_404(setup):
    server = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.patch(
            "/api/sessions/nope", json={"notes": "anything"}
        )
    assert resp.status_code == 404


async def test_session_notes_other_user_gets_404(setup):
    """A session owned by chat_id=999 must not be editable by user 100."""
    server = setup
    sm = server.session_manager
    other = sm.create_session(
        topic_id=600,
        project_path=str(server.project_paths[0]),
        project_name="someone-else",
        chat_id=999,  # NOT the test user (100)
    )

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)  # logs in as user 100
        resp = await client.patch(
            f"/api/sessions/{other.session_uuid}",
            json={"notes": "trying to vandalize"},
        )
    assert resp.status_code == 404
    # Confirm notes were NOT applied
    assert sm.get_session(600).notes is None


async def test_sessions_search_uses_latest_message_only(setup):
    """Earlier matching content in a session must NOT match if the *latest*
    message doesn't contain the substring."""
    server = setup
    sm = server.session_manager
    sm.create_session(
        topic_id=500,
        project_path=str(server.project_paths[0]),
        project_name="proj-x",
    )
    conn = sm._get_connection()
    # Older message contains 'needle'; the LAST message does not.
    conn.execute(
        "INSERT INTO messages (topic_id, type, kind, content, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (500, "user_message", None, "needle in the haystack"),
    )
    conn.execute(
        "INSERT INTO messages (topic_id, type, kind, content, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now'))",
        (500, "user_message", None, "later unrelated text"),
    )
    conn.commit()

    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await _login(client)
        resp = await client.get("/api/sessions?q=needle")
    assert resp.status_code == 200
    topic_ids = {s["topic_id"] for s in resp.json()}
    assert 500 not in topic_ids
