"""REST /api/connections: каталог + connect/disconnect, per-user, без секретов.

Harness (login + authed TestClient) скопирован из tests/web/test_stop_generation.py:
Telegram-логин через /api/auth/telegram (HMAC-подпись по bot_token) ставит cookie
vels_session, дальше TestClient шлёт её автоматически.

КРИТИЧНО: GET никогда не возвращает секреты — только `connected` booleans.
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

from src.connections.crypto import SecretBox
from src.connections.store import ConnectionsStore
from src.config.settings import WebSettings
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


def _make_store(tmp_dir: Path) -> ConnectionsStore:
    box = SecretBox(Fernet.generate_key().decode())
    return ConnectionsStore(tmp_dir / "connections.db", box)


def _make_server(connections_store) -> WebServer:
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
        connections_store=connections_store,
    )


def _login(client: TestClient, user_id: int = 100) -> None:
    login = _sign(
        "12345:abc",
        {"id": user_id, "first_name": "A", "auth_date": int(time.time())},
    )
    resp = client.post("/api/auth/telegram", json=login)
    assert resp.status_code == 200, resp.text


def test_list_connect_disconnect_flow(tmp_dir):
    store = _make_store(tmp_dir)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)

            # GET: enabled, оба сервиса в каталоге, оба not connected.
            resp = client.get("/api/connections")
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["enabled"] is True
            by_id = {s["id"]: s for s in body["services"]}
            assert "notion" in by_id
            assert "github" in by_id
            assert by_id["github"]["connected"] is False
            assert by_id["notion"]["connected"] is False
            # Секреты не утекают ни под каким ключом.
            for svc in body["services"]:
                assert "secret" not in svc
                assert "secret_encrypted" not in svc

            # POST: подключаем github.
            resp = client.post(
                "/api/connections",
                json={"service_id": "github", "secret": "PAT"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True}

            # GET: github теперь connected.
            resp = client.get("/api/connections")
            assert resp.status_code == 200
            by_id = {s["id"]: s for s in resp.json()["services"]}
            assert by_id["github"]["connected"] is True
            assert by_id["notion"]["connected"] is False

            # POST неизвестного сервиса → 422.
            resp = client.post(
                "/api/connections",
                json={"service_id": "nope", "secret": "x"},
            )
            assert resp.status_code == 422, resp.text

            # DELETE: отключаем github → 200.
            resp = client.delete("/api/connections/github")
            assert resp.status_code == 200, resp.text

            # GET: github снова not connected.
            resp = client.get("/api/connections")
            by_id = {s["id"]: s for s in resp.json()["services"]}
            assert by_id["github"]["connected"] is False

            # Повторный DELETE → 404 (уже не подключён).
            resp = client.delete("/api/connections/github")
            assert resp.status_code == 404, resp.text
    finally:
        store.close()


def test_get_never_returns_secret(tmp_dir):
    store = _make_store(tmp_dir)
    server = _make_server(store)
    try:
        with TestClient(server.app) as client:
            _login(client)
            client.post(
                "/api/connections",
                json={"service_id": "github", "secret": "super-secret-token"},
            )
            resp = client.get("/api/connections")
            assert resp.status_code == 200
            assert "super-secret-token" not in resp.text
    finally:
        store.close()


def test_disabled_when_store_none(tmp_dir):
    server = _make_server(None)
    with TestClient(server.app) as client:
        _login(client)

        resp = client.get("/api/connections")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        resp = client.post(
            "/api/connections",
            json={"service_id": "github", "secret": "PAT"},
        )
        assert resp.status_code == 503, resp.text


def test_ws_usermessage_has_user_id_kwarg():
    import ast
    import inspect
    from src.web import routes_ws
    tree = ast.parse(inspect.getsource(routes_ws))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "UserMessageReceived"
    ]
    assert calls, "no UserMessageReceived(...) call found in routes_ws"
    for c in calls:
        kwargs = {k.arg for k in c.keywords if k.arg}
        assert "user_id" in kwargs, "web UserMessageReceived must set user_id (per-user MCP)"
