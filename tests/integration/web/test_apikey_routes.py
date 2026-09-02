"""REST /api/apikey: per-user Anthropic API key metadata + set/delete.

Harness (Telegram login + authed TestClient) mirrors
tests/web/test_connections_routes.py: /api/auth/telegram sets the vels_session
cookie, then TestClient replays it on every request.

CRITICAL invariants under test:
* GET returns only metadata (status/last4/timestamps) — never the full key.
* PUT validates the offline *format* first, then live-*probes* the key, mapping
  VALID→active, INVALID→422, UNKNOWN→saved-but-unverified.
* api_key_store=None (feature off) → 501 on every verb.
"""
from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

from src.apikeys.crypto import ApiKeyCrypto
from src.apikeys.store import ApiKeyStore
from src.apikeys.validate import ProbeResult
from src.config.settings import WebSettings
from src.web import routes_apikey
from src.web.server import WebServer

# A real-looking key: "sk-ant-" + >=40 chars from [a-zA-Z0-9-_]. The SECRETMIDDLE
# marker lets tests assert the *full* key never leaks, while last4 ("0000") may.
VALID_KEY = "sk-ant-api03-SECRETMIDDLE" + "0" * 40


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


def _make_store(tmp_dir: Path) -> ApiKeyStore:
    crypto = ApiKeyCrypto(Fernet.generate_key().decode())
    return ApiKeyStore(tmp_dir / "sessions.db", crypto)


def _make_server(api_key_store) -> WebServer:
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    return WebServer(
        settings=settings,
        allowed_user_ids=[100],
        bot_username="t",
        session_manager=None,
        event_bus=None,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
        api_key_store=api_key_store,
    )


def _login(client: TestClient, user_id: int = 100) -> None:
    login = _sign(
        "12345:abc",
        {"id": user_id, "first_name": "A", "auth_date": int(time.time())},
    )
    resp = client.post("/api/auth/telegram", json=login)
    assert resp.status_code == 200, resp.text


def _patch_probe(monkeypatch, result: ProbeResult) -> None:
    async def _fake_probe(key: str, timeout: float = 10.0) -> ProbeResult:
        return result

    monkeypatch.setattr(routes_apikey, "probe_key", _fake_probe)


# --------------------------------------------------------------------------- #
# GET
# --------------------------------------------------------------------------- #
def test_get_returns_metadata_when_present(tmp_dir):
    store = _make_store(tmp_dir)
    store.set_key(100, VALID_KEY, status="active")
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            resp = client.get("/api/apikey")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "active"
            assert body["last4"] == "0000"
            assert body["created_at"]
            assert body["updated_at"]
            assert isinstance(body["privileged"], bool)
            # The full key never appears in the metadata response.
            assert "SECRETMIDDLE" not in resp.text
            assert VALID_KEY not in resp.text
    finally:
        store.close()


def test_get_returns_none_when_absent(tmp_dir):
    store = _make_store(tmp_dir)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            resp = client.get("/api/apikey")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] is None
            assert body["last4"] is None
            assert body["created_at"] is None
            assert body["updated_at"] is None
            assert isinstance(body["privileged"], bool)
    finally:
        store.close()


def test_get_requires_auth(tmp_dir):
    store = _make_store(tmp_dir)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            resp = client.get("/api/apikey")
            assert resp.status_code == 401, resp.text
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# PUT
# --------------------------------------------------------------------------- #
def test_put_valid_format_and_probe_ok_saves_active(tmp_dir, monkeypatch):
    store = _make_store(tmp_dir)
    _patch_probe(monkeypatch, ProbeResult.VALID)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            resp = client.put("/api/apikey", json={"api_key": VALID_KEY})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "active"
            assert body["message"] == "API key saved"
            # Persisted, and retrievable/decryptable as the real key.
            assert store.get_key(100) == VALID_KEY
            meta = store.get_meta(100)
            assert meta["status"] == "active"
            assert meta["last4"] == "0000"
    finally:
        store.close()


def test_put_invalid_format_returns_422(tmp_dir, monkeypatch):
    store = _make_store(tmp_dir)
    # Probe must never be reached for a malformed key; make it explode if it is.
    async def _boom(key: str, timeout: float = 10.0):
        raise AssertionError("probe_key must not run for an invalid format")

    monkeypatch.setattr(routes_apikey, "probe_key", _boom)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            resp = client.put("/api/apikey", json={"api_key": "not-a-real-key"})
            assert resp.status_code == 422, resp.text
            assert "format" in resp.json()["detail"].lower()
            # Nothing persisted.
            assert store.get_meta(100) is None
    finally:
        store.close()


def test_put_probe_invalid_returns_422_and_saves_nothing(tmp_dir, monkeypatch):
    store = _make_store(tmp_dir)
    _patch_probe(monkeypatch, ProbeResult.INVALID)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            resp = client.put("/api/apikey", json={"api_key": VALID_KEY})
            assert resp.status_code == 422, resp.text
            assert "401" in resp.json()["detail"]
            # A rejected key must not be persisted.
            assert store.get_meta(100) is None
    finally:
        store.close()


def test_put_probe_unknown_saves_unverified(tmp_dir, monkeypatch):
    store = _make_store(tmp_dir)
    _patch_probe(monkeypatch, ProbeResult.UNKNOWN)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            resp = client.put("/api/apikey", json={"api_key": VALID_KEY})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "unverified"
            assert body["message"] == "API key saved"
            # Saved despite the inconclusive probe, flagged unverified.
            assert store.get_key(100) == VALID_KEY
            assert store.get_meta(100)["status"] == "unverified"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# DELETE
# --------------------------------------------------------------------------- #
def test_delete_removes_key(tmp_dir):
    store = _make_store(tmp_dir)
    store.set_key(100, VALID_KEY, status="active")
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            assert store.get_meta(100) is not None
            resp = client.delete("/api/apikey")
            assert resp.status_code == 200, resp.text
            assert resp.json()["message"] == "API key deleted"
            assert store.get_meta(100) is None
            # GET now reports no key.
            resp = client.get("/api/apikey")
            assert resp.json()["status"] is None
    finally:
        store.close()


def test_delete_is_idempotent(tmp_dir):
    store = _make_store(tmp_dir)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            resp = client.delete("/api/apikey")
            assert resp.status_code == 200, resp.text
            assert resp.json()["message"] == "API key deleted"
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Feature disabled (store not configured) → 501
# --------------------------------------------------------------------------- #
def test_all_verbs_501_when_store_none():
    server = _make_server(None)
    with TestClient(server.app) as client:
        _login(client)

        resp = client.get("/api/apikey")
        assert resp.status_code == 501, resp.text

        resp = client.put("/api/apikey", json={"api_key": VALID_KEY})
        assert resp.status_code == 501, resp.text

        resp = client.delete("/api/apikey")
        assert resp.status_code == 501, resp.text
