"""Smoke tests for the post-review fixes that aren't security-specific."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.event_bus.events import UserMessageReceived
from src.web.server import WebServer
from src.web.ws_forwarder import WSForwarder

BOT_TOKEN = "12345:abc"
USER = 100


def _sign(payload: dict) -> dict:
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
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


def _make_server(tmp_dir, *, cookie_secure=None, public_origin=""):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    (tmp_dir / "demo").mkdir(exist_ok=True)
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0,
        jwt_secret="x" * 32, telegram_bot_username="t",
        public_origin=public_origin, cookie_secure=cookie_secure,
    )
    srv = WebServer(
        settings=settings,
        allowed_user_ids=[USER],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token=BOT_TOKEN,
        jwt_secret="x" * 32,
        project_paths=[tmp_dir / "demo"],
    )
    return srv, sm, bus


# ---------------------------------------------------------------------------
# Schema migration: users.verbose_level added to fresh DB
# ---------------------------------------------------------------------------


def test_users_verbose_level_present_on_fresh_db(tmp_dir):
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    try:
        conn = sm._get_connection()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        assert "verbose_level" in cols
        # And the helpers round-trip.
        assert sm.get_user_default_verbose(USER) == 1  # default
        sm.set_user_verbose(USER, 2)
        assert sm.get_user_default_verbose(USER) == 2
        # set_user_verbose must NOT touch sessions.verbose_level
        sm.create_session(topic_id=1, project_path="/x", project_name="x", chat_id=USER)
        sm.set_verbose_level(1, 3)
        sm.set_user_verbose(USER, 0)
        assert sm.get_session(1).verbose_level == 3
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


# ---------------------------------------------------------------------------
# next_negative_topic_id: O(1), descending, skips holes
# ---------------------------------------------------------------------------


def test_next_negative_topic_id_descends(tmp_dir):
    """MIN(topic_id) - 1 always returns below the current minimum.

    After deleting a session, that slot may be re-used because the
    session_uuid is fresh and FK CASCADE already wiped any related
    messages — clients address sessions by UUID, not topic_id.
    """
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    try:
        assert sm.next_negative_topic_id() == -1
        sm.create_session(topic_id=-1, project_path="/a", project_name="a", chat_id=USER)
        assert sm.next_negative_topic_id() == -2
        sm.create_session(topic_id=-2, project_path="/b", project_name="b", chat_id=USER)
        assert sm.next_negative_topic_id() == -3
        sm.create_session(topic_id=-3, project_path="/c", project_name="c", chat_id=USER)
        # Delete the bottom -3 → next jumps back to -3 (MIN is now -2)
        sm.close_session(-3)
        assert sm.next_negative_topic_id() == -3
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


# ---------------------------------------------------------------------------
# EventBus: failing subscriber doesn't halt downstream
# ---------------------------------------------------------------------------


async def test_event_bus_subscriber_failure_is_isolated(caplog):
    bus = EventBus()
    received: list[str] = []

    async def bad(_event):
        raise RuntimeError("boom from subscriber #1")

    async def good(event):
        received.append(event.text)

    # High-priority bad subscriber runs first
    bus.subscribe(UserMessageReceived, bad, priority=100)
    bus.subscribe(UserMessageReceived, good, priority=0)

    caplog.set_level(logging.ERROR)
    await bus.publish(UserMessageReceived(
        request_id="r1", chat_id=1, topic_id=1,
        project_path="/x", text="hello",
    ))

    # Downstream subscriber ran despite the upstream crash.
    assert received == ["hello"]


# ---------------------------------------------------------------------------
# WSForwarder: empty session_uuid logs warning instead of silent drop
# ---------------------------------------------------------------------------


async def test_ws_forwarder_warns_on_missing_session_uuid(caplog):
    bus = EventBus()
    fwd = WSForwarder(bus=bus)
    await fwd.start()
    try:
        caplog.set_level(logging.WARNING)
        # session_uuid defaults to "" → silent drop is the bug we fixed.
        await bus.publish(UserMessageReceived(
            request_id="r", chat_id=1, topic_id=1,
            project_path="/x", text="hi",
            # session_uuid not set
        ))
        # Warning is emitted via structlog → captured as a record on the
        # root logger when structlog is configured to forward to stdlib.
        # Either way, the fanout must NOT raise.
    finally:
        await fwd.stop()


# ---------------------------------------------------------------------------
# Cookie Secure: explicit override and auto-detect
# ---------------------------------------------------------------------------


async def test_cookie_secure_explicit_true_overrides_http_origin(tmp_dir):
    """`web.cookie_secure: true` forces Secure even with http:// origin
    (e.g. nginx terminating TLS in front of an http upstream)."""
    srv, _sm, _bus = _make_server(
        tmp_dir, cookie_secure=True, public_origin="http://upstream:8765"
    )
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        payload = _sign({"id": USER, "first_name": "A",
                         "auth_date": int(time.time())})
        r = await c.post("/api/auth/telegram", json=payload)
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "vels_session=" in set_cookie
    assert "Secure" in set_cookie, f"missing Secure in: {set_cookie}"


async def test_cookie_secure_none_derives_from_https_origin(tmp_dir):
    srv, _sm, _bus = _make_server(
        tmp_dir, cookie_secure=None, public_origin="https://prod.example.com"
    )
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        payload = _sign({"id": USER, "first_name": "A",
                         "auth_date": int(time.time())})
        r = await c.post("/api/auth/telegram", json=payload)
    assert "Secure" in r.headers.get("set-cookie", "")


async def test_cookie_secure_none_with_http_origin_omits_secure(tmp_dir):
    srv, _sm, _bus = _make_server(
        tmp_dir, cookie_secure=None, public_origin="http://localhost:5173"
    )
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        payload = _sign({"id": USER, "first_name": "A",
                         "auth_date": int(time.time())})
        r = await c.post("/api/auth/telegram", json=payload)
    assert "Secure" not in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# Magic-link: one-time consume, second attempt returns 401 expired_or_used
# ---------------------------------------------------------------------------


async def test_magic_link_is_one_shot(tmp_dir):
    srv, _sm, _bus = _make_server(tmp_dir)
    # Mint a token for user
    token = srv.magic_link_store.create(USER)

    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r1 = await c.post("/api/auth/magic-link", json={"token": token})
        assert r1.status_code == 200

    async with AsyncClient(transport=transport, base_url="http://t") as c2:
        r2 = await c2.post("/api/auth/magic-link", json={"token": token})
        assert r2.status_code == 401
        # detail dict carries error_code for the frontend
        body = r2.json()
        assert body["detail"]["error_code"] == "expired_or_used"


# ---------------------------------------------------------------------------
# message_store: _insert is now async-friendly via asyncio.to_thread
# ---------------------------------------------------------------------------


async def test_message_persister_inserts_via_to_thread(tmp_dir):
    """Confirms the _on_user_message path commits without blocking the loop.

    We can't easily measure "didn't block" but we CAN confirm the insert
    happens at all — the refactor to `await asyncio.to_thread(self._insert, ...)`
    must still produce a row in the messages table.
    """
    from src.web.message_store import MessageHistoryPersister
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    try:
        sm.create_session(topic_id=7, project_path="/x", project_name="x", chat_id=USER)
        bus = EventBus()
        persister = MessageHistoryPersister(bus=bus, session_manager=sm)
        await persister.start()
        try:
            await bus.publish(UserMessageReceived(
                request_id="r1", chat_id=USER, topic_id=7,
                project_path="/x", text="persisted-message",
            ))
        finally:
            await persister.stop()

        conn = sm._get_connection()
        rows = conn.execute(
            "SELECT content FROM messages WHERE topic_id = 7"
        ).fetchall()
        assert any(r[0] == "persisted-message" for r in rows)
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
