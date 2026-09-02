"""Tests for /api/auth/* and /api/me endpoints."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from httpx import ASGITransport, AsyncClient

from src.config.settings import WebSettings
from src.web.server import WebServer


def _sign(bot_token: str, payload: dict) -> dict:
    secret = hashlib.sha256(bot_token.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


@pytest.fixture
def server():
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
        jwt_ttl_days=30,
        telegram_bot_username="testbot",
    )
    return WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="testbot",
        session_manager=None,
        event_bus=None,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
    )


async def test_login_success_sets_cookie(server):
    payload = _sign(
        "12345:abc",
        {"id": 100, "first_name": "A", "auth_date": int(time.time())},
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/telegram", json=payload)
    assert resp.status_code == 200
    assert "vels_session" in resp.cookies


async def test_login_rejects_unknown_user(server):
    payload = _sign(
        "12345:abc",
        {"id": 999, "first_name": "X", "auth_date": int(time.time())},
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/telegram", json=payload)
    assert resp.status_code == 403


async def test_logout_clears_cookie(server):
    payload = _sign(
        "12345:abc",
        {"id": 100, "first_name": "A", "auth_date": int(time.time())},
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/auth/telegram", json=payload)
        resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert "vels_session=" in set_cookie
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


async def test_me_endpoint_requires_auth(server):
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/me")
    assert resp.status_code == 401


async def test_me_endpoint_returns_user(server):
    payload = _sign(
        "12345:abc",
        {"id": 100, "first_name": "Alice", "auth_date": int(time.time())},
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/auth/telegram", json=payload)
        resp = await client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["id"] == 100


# --- Dev-login (Task 3.3) -----------------------------------------------------


@pytest.fixture
def server_with_bearer():
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
        jwt_ttl_days=30,
        telegram_bot_username="testbot",
        dev_bearer_token="dev-secret-token",
    )
    return WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="testbot",
        session_manager=None,
        event_bus=None,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
        dev_bearer_token="dev-secret-token",
    )


async def test_dev_login_success(server_with_bearer):
    transport = ASGITransport(app=server_with_bearer.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/dev-login", json={"token": "dev-secret-token"}
        )
    assert resp.status_code == 200
    assert "vels_session" in resp.cookies


async def test_dev_login_wrong_token_rejected(server_with_bearer):
    transport = ASGITransport(app=server_with_bearer.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/dev-login", json={"token": "wrong"})
    assert resp.status_code == 401


async def test_dev_login_disabled_when_token_unset(server):
    """The default `server` fixture has no dev_bearer_token — dev-login must 404."""
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/dev-login", json={"token": "anything"})
    assert resp.status_code == 404


async def test_dev_login_disabled_by_flag_even_with_token():
    """H-6: web.dev_login_enabled=False → /api/auth/dev-login = 404, даже если
    bearer-токен задан и верный (kill-switch для прода)."""
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        jwt_ttl_days=30, telegram_bot_username="testbot",
        dev_bearer_token="dev-secret-token", dev_login_enabled=False,
    )
    srv = WebServer(
        settings=settings, allowed_user_ids=[100], bot_username="testbot",
        session_manager=None, event_bus=None, bot_token="12345:abc",
        jwt_secret="x" * 32, dev_bearer_token="dev-secret-token",
    )
    transport = ASGITransport(app=srv.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/auth/dev-login", json={"token": "dev-secret-token"}
        )
    assert resp.status_code == 404
