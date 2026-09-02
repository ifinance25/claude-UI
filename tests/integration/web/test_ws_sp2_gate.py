"""WS seam regression: routes_ws.py -> ClaudeEventRelay SP2 gate (audit HIGH).

The web SP2 gate has two halves that only meet in production:

* ``routes_ws.py`` decides, per message, whether the caller is *privileged*
  (owner/admin/whitelist ride owner credentials) or not, and threads that onto
  the published ``UserMessageReceived(privileged=...)`` (see the ``privileged``
  computation around routes_ws.py:458-481 and the publish at :516); and
* :class:`~src.event_bus.claude.ClaudeEventRelay` reads ``event.privileged`` and
  fail-closed-resolves which Anthropic key the run uses.

The relay half is covered by ``tests/integration/event_bus/test_claude_relay.py``
— but it hand-builds ``UserMessageReceived`` and so would keep passing even if
``routes_ws.py`` stopped computing ``privileged`` correctly. This test closes
that seam: it drives the **real** WebSocket chat path (login -> WS upgrade ->
``user_message`` frame) through ``routes_ws.py`` so the router's *own* privilege
computation runs, with a real relay (spy bridge) on the same bus resolving auth.

Cases:
  (a) unprivileged local user, no stored key, require_user_key=True + store
      present -> NEEDS_API_KEY refusal surfaced, spy bridge NEVER spawned;
  (b) same user after a key is stored -> that key injected into the run;
  (c) whitelisted user, no key -> owner mode (run proceeds, no key injected).

If routes_ws.py regressed to publishing ``privileged=False`` for the whitelisted
user, case (c) would flip to a refusal (spy never called, finished.error set).
If it published ``privileged=True`` for the unprivileged user, case (a) would
run under owner creds instead of refusing. Both are caught here.
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
from src.claude.bridge import ClaudeEvent, ClaudeEventType
from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus import ClaudeEventRelay, EventBus
from src.event_bus.claude import NEEDS_API_KEY_MESSAGE
from src.web.passwords import hash_password
from src.web.server import WebServer

USER_KEY = "sk-ant-api03-USERkey-ws-seam-1234567890"
WHITELISTED_ID = 100


def _sign(bot_token: str, payload: dict) -> dict:
    secret = hashlib.sha256(bot_token.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


class _SpyBridge:
    """Relay's Claude bridge stand-in: records send_message kwargs, no spawn."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        yield ClaudeEvent(
            ClaudeEventType.COMPLETE,
            metadata={"session_id": "sdk-session", "usage": {"input_tokens": 1}},
        )


class _FakeSettingsForRelay:
    def __init__(self, require_user_key: bool) -> None:
        self.require_user_key = require_user_key


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _make_store(tmp_dir: Path) -> ApiKeyStore:
    crypto = ApiKeyCrypto(Fernet.generate_key().decode())
    return ApiKeyStore(tmp_dir / "apikeys.db", crypto)


def _make_server(bus: EventBus, sm: SessionManager, store: ApiKeyStore | None) -> WebServer:
    settings = WebSettings(
        enabled=True,
        host="127.0.0.1",
        port=0,
        jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    return WebServer(
        settings=settings,
        allowed_user_ids=[WHITELISTED_ID],
        bot_username="t",
        session_manager=sm,
        event_bus=bus,
        bot_token="12345:abc",
        jwt_secret="x" * 32,
        api_key_store=store,
    )


def _drive(ws) -> list[dict]:
    """Send one user_message and collect frames up to (and including) 'finished'."""
    ws.send_json({"type": "user_message", "text": "hi"})
    frames: list[dict] = []
    for _ in range(12):
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") == "finished":
            return frames
    raise AssertionError(
        f"no 'finished' frame; got {[f.get('type') for f in frames]}"
    )


def _finished(frames: list[dict]) -> dict:
    return next(f for f in frames if f.get("type") == "finished")


# --------------------------------------------------------------------------- #
# (a) unprivileged local user, no key -> refusal, no spawn.
# (b) same user, key stored          -> key injected.
# --------------------------------------------------------------------------- #
def test_ws_unprivileged_local_user_refused_then_injected(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    store = _make_store(tmp_dir)
    spy = _SpyBridge()
    relay = ClaudeEventRelay(
        bus=bus,
        claude_bridge=spy,
        api_key_store=store,
        settings=_FakeSettingsForRelay(require_user_key=True),
    )
    # A non-whitelisted, non-admin local account (unprivileged).
    uid = sm.create_local_user(
        username="alice", password_hash=hash_password("pw"), is_admin=False
    )
    # A no-project ("clean Claude") session owned by that user.
    session = sm.create_session(
        topic_id=-101, project_path="", project_name="", chat_id=uid
    )
    server = _make_server(bus, sm, store)

    try:
        with TestClient(server.app) as client:
            resp = client.post(
                "/api/auth/login", json={"username": "alice", "password": "pw"}
            )
            assert resp.status_code == 200, resp.text

            # (a) No stored key + require_user_key -> refusal, bridge NOT spawned.
            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                frames = _drive(ws)
            fin = _finished(frames)
            assert fin["error"] == NEEDS_API_KEY_MESSAGE
            assert spy.calls == [], "unprivileged no-key run must NOT spawn Claude"

            # (b) Store a key for this user -> the relay injects exactly that key.
            store.set_key(uid, USER_KEY)
            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                frames = _drive(ws)
            fin = _finished(frames)
            assert fin["error"] is None
            assert len(spy.calls) == 1
            assert spy.calls[0]["anthropic_api_key"] == USER_KEY
    finally:
        relay.close()
        store.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()


# --------------------------------------------------------------------------- #
# (c) whitelisted user, no key -> owner mode (run proceeds, no key injected).
# This is the assertion that catches routes_ws.py dropping privilege: a
# whitelisted caller must be recognised as privileged and NOT refused.
# --------------------------------------------------------------------------- #
def test_ws_whitelisted_user_without_key_runs_owner_mode(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    store = _make_store(tmp_dir)
    spy = _SpyBridge()
    relay = ClaudeEventRelay(
        bus=bus,
        claude_bridge=spy,
        api_key_store=store,
        settings=_FakeSettingsForRelay(require_user_key=True),
    )
    session = sm.create_session(
        topic_id=-102, project_path="", project_name="", chat_id=WHITELISTED_ID
    )
    server = _make_server(bus, sm, store)

    try:
        with TestClient(server.app) as client:
            login = _sign(
                "12345:abc",
                {"id": WHITELISTED_ID, "first_name": "A", "auth_date": int(time.time())},
            )
            resp = client.post("/api/auth/telegram", json=login)
            assert resp.status_code == 200, resp.text

            with client.websocket_connect(
                f"/api/ws/sessions/{session.session_uuid}"
            ) as ws:
                frames = _drive(ws)
            fin = _finished(frames)
            # Not refused: the run proceeded under owner credentials (no key).
            assert fin["error"] is None
            assert len(spy.calls) == 1
            assert spy.calls[0]["anthropic_api_key"] is None
    finally:
        relay.close()
        store.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()
