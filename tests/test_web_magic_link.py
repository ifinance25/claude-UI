"""Tests for the magic-link auth flow.

Covers:
- MagicLinkStore (create/consume/expire/one-time semantics)
- POST /api/auth/magic-link (success, 401 paths)
- WebServer.issue_magic_login_url URL composition (public_origin
  honoured; fallback to 127.0.0.1 when absent, with warning)
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.magic_link import MagicLinkStore
from src.web.server import WebServer


# --- MagicLinkStore unit tests ----------------------------------------------


def test_create_returns_unique_tokens():
    store = MagicLinkStore(ttl_seconds=60)
    a = store.create(user_id=100)
    b = store.create(user_id=100)
    assert a != b
    assert len(a) >= 20  # token_urlsafe(24) -> ~32 chars


def test_consume_valid_token_returns_user_id():
    store = MagicLinkStore(ttl_seconds=60)
    token = store.create(user_id=42, now=1000.0)
    assert store.consume(token, now=1001.0) == 42


def test_consume_is_one_time():
    store = MagicLinkStore(ttl_seconds=60)
    token = store.create(user_id=42, now=1000.0)
    assert store.consume(token, now=1001.0) == 42
    assert store.consume(token, now=1002.0) is None


def test_consume_expired_token_returns_none():
    store = MagicLinkStore(ttl_seconds=60)
    token = store.create(user_id=42, now=1000.0)
    assert store.consume(token, now=2000.0) is None


def test_consume_unknown_token_returns_none():
    store = MagicLinkStore(ttl_seconds=60)
    assert store.consume("never-issued") is None
    assert store.consume("") is None


def test_create_evicts_expired_entries():
    store = MagicLinkStore(ttl_seconds=60)
    old = store.create(user_id=1, now=1000.0)
    # Mint a new token well past the TTL — old must be evicted by lazy GC
    store.create(user_id=2, now=2000.0)
    assert old not in store._tokens
    assert store.consume(old, now=2001.0) is None


# --- POST /api/auth/magic-link integration tests -----------------------------


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def server(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=8765,
        jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    srv = WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
    )
    try:
        yield srv
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_magic_link_valid_token_sets_cookie(server):
    token = server.magic_link_store.create(user_id=100)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/magic-link", json={"token": token})
    assert resp.status_code == 200
    assert "vels_session" in resp.cookies
    assert resp.json()["user"]["id"] == 100


async def test_magic_link_token_is_single_use(server):
    token = server.magic_link_store.create(user_id=100)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/auth/magic-link", json={"token": token})
        assert first.status_code == 200
        second = await client.post("/api/auth/magic-link", json={"token": token})
    assert second.status_code == 401
    body = second.json()
    assert body["detail"]["error_code"] == "expired_or_used"


async def test_magic_link_expired_token_rejected(server):
    token = server.magic_link_store.create(user_id=100, now=1000.0)
    # Force expiry by advancing the consumer's clock past TTL
    # (TTL default 15 min = 900s; jump 1000 + 1000 = 2000 is well past).
    # We can't pass `now` through the route, so use a small TTL store
    # instance directly to verify expiry semantics.
    short = MagicLinkStore(ttl_seconds=1)
    short_token = short.create(user_id=100, now=1000.0)
    assert short.consume(short_token, now=1002.0) is None

    # And via the route: consume the original token to remove it, then
    # show the route returns 401 on a fresh unknown one.
    server.magic_link_store.consume(token)
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/magic-link", json={"token": "unknown-token"}
        )
    assert resp.status_code == 401


async def test_magic_link_unknown_token_rejected(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/magic-link", json={"token": "junk"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error_code"] == "expired_or_used"


async def test_magic_link_empty_token_rejected(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/magic-link", json={"token": ""})
    assert resp.status_code == 401


async def test_magic_link_user_not_in_whitelist_rejected(server):
    """If the whitelist tightens after a token was minted, refuse it."""
    token = server.magic_link_store.create(user_id=999)  # not in [100]
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/magic-link", json={"token": token})
    assert resp.status_code == 403


# --- WebServer.issue_magic_login_url -----------------------------------------


def test_issue_login_url_with_public_origin(server):
    object.__setattr__(server.settings, "public_origin", "https://vels.example.com/")
    url = server.issue_magic_login_url(user_id=100)
    assert url.startswith("https://vels.example.com/login?magic=")
    # No trailing slash duplication
    assert "vels.example.com//" not in url


def test_issue_login_url_without_public_origin_warns(server, monkeypatch):
    object.__setattr__(server.settings, "public_origin", "")
    captured: list[tuple[str, dict]] = []
    import src.web.server as server_module

    def fake_warning(event, **kw):
        captured.append((event, kw))

    monkeypatch.setattr(server_module.logger, "warning", fake_warning)
    url = server.issue_magic_login_url(user_id=100)
    assert url.startswith(f"http://127.0.0.1:{server.settings.port}/login?magic=")
    assert any(event == "weblogin_no_public_origin" for event, _ in captured)
