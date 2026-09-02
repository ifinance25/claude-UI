"""End-to-end web flow with a fake Claude.

Validates bidirectional sync without a real Claude Code CLI or Telegram:
  Web client opens WS
  Web client sends user_message
  WS reader publishes UserMessageReceived on the bus
  FakeClaude (subscribed) generates AgentStarted + text deltas + AgentFinished
  WSForwarder fans out each event back to the WS client
  MessageHistoryPersister writes every event to the messages table
  REST /api/sessions/:topic_id/messages returns the full history
"""
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.event_bus.events import (
    AgentFinished,
    AgentStarted,
    AgentStreamingUpdate,
    UserMessageReceived,
)
from src.web.message_store import MessageHistoryPersister
from src.web.server import WebServer


def _sign(bot_token: str, payload: dict) -> dict:
    secret = hashlib.sha256(bot_token.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


class FakeClaude:
    """Subscribes to UserMessageReceived and emits a scripted Claude response."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self._unsub = bus.subscribe(UserMessageReceived, self._handle)

    def close(self) -> None:
        self._unsub()

    async def _handle(self, event: UserMessageReceived) -> None:
        await self.bus.publish(
            AgentStarted(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
            )
        )
        for ch in "Hi!":
            await self.bus.publish(
                AgentStreamingUpdate(
                    request_id=event.request_id,
                    chat_id=event.chat_id,
                    topic_id=event.topic_id,
                    session_uuid=event.session_uuid,
                    kind="text",
                    content=ch,
                )
            )
        await self.bus.publish(
            AgentFinished(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
                response_text="Hi!",
                usage={
                    "input_tokens": 5,
                    "output_tokens": 3,
                    "cost_usd": 0.001,
                },
            )
        )


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_full_web_flow(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    (tmp_dir / "demo").mkdir()

    fake_claude = FakeClaude(bus)
    persister = MessageHistoryPersister(
        bus=bus, session_manager=sm, text_flush_interval_ms=10
    )

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
        with TestClient(server.app) as client:
            # Start persister inside the server's loop so its flush_loop task
            # runs there and sees publishes from the WS handler.
            client.portal.call(persister.start)

            # Login (dev path would also work; we use Telegram for parity)
            login = _sign(
                "12345:abc",
                {"id": 100, "first_name": "A", "auth_date": int(time.time())},
            )
            resp = client.post("/api/auth/telegram", json=login)
            assert resp.status_code == 200

            # Create web session (negative topic_id, but use session_uuid for routing)
            resp = client.post(
                "/api/sessions",
                json={
                    "project_path": str(tmp_dir / "demo"),
                    "project_name": "demo",
                },
            )
            assert resp.status_code == 201
            body = resp.json()
            topic_id = body["topic_id"]
            session_uuid = body["session_uuid"]
            assert topic_id < 0
            assert len(session_uuid) == 32

            received: list[dict] = []
            with client.websocket_connect(
                f"/api/ws/sessions/{session_uuid}"
            ) as ws:
                ws.send_text(
                    json.dumps({"type": "user_message", "text": "Say hi"})
                )

                # Collect events until 'finished' (cap iterations as safety)
                for _ in range(20):
                    msg = ws.receive_json()
                    received.append(msg)
                    if msg["type"] == "finished":
                        break

            # --- WS-side assertions ---
            # WSForwarder.start() subscribes UserMessageReceived with
            # priority=100, so the echo arrives BEFORE FakeClaude (which
            # subscribes with default priority=0) begins publishing its
            # reply chain. So the WS stream order is:
            #   user_message -> agent_started -> text* -> finished
            types = [m["type"] for m in received]
            assert "user_message" in types
            assert "agent_started" in types
            user_msg = next(m for m in received if m["type"] == "user_message")
            assert user_msg["content"] == "Say hi"
            assert user_msg.get("source") == "web"
            # Order check: user_message comes before any agent_started/text/finished
            assert types.index("user_message") < types.index("agent_started")
            text_events = [
                m for m in received
                if m["type"] == "streaming_update" and m["kind"] == "text"
            ]
            assert text_events, "expected at least one text delta in WS stream"
            assert "".join(m["content"] for m in text_events) == "Hi!"
            assert types[-1] == "finished"
            finished = received[-1]
            assert finished["response_text"] == "Hi!"
            assert finished["usage"]["cost_usd"] == 0.001

            # Drain persister so all text-delta batches hit the DB
            client.portal.call(persister.flush)
            client.portal.call(persister.stop)

        # --- DB-side assertions ---
        conn = sm._get_connection()
        rows = conn.execute(
            "SELECT type, kind, content FROM messages "
            "WHERE topic_id = ? ORDER BY event_id",
            (topic_id,),
        ).fetchall()
        types_in_db = [r[0] for r in rows]
        assert "user_message" in types_in_db
        assert any(
            r[0] == "user_message" and r[2] == "Say hi" for r in rows
        )
        assert any(
            r[0] == "streaming_update" and r[1] == "text" and "Hi!" in r[2]
            for r in rows
        ), f"expected batched text='Hi!' in DB, got: {rows}"
        assert any(r[0] == "finished" for r in rows)

        # --- REST recovery path: GET /messages returns the same history ---
        # Re-open a client to fetch history independently of the WS session.
        with TestClient(server.app) as client2:
            # Отдельный логин-флоу нового клиента: используем иной auth_date,
            # иначе payload совпал бы с первым входом по hash и был бы отклонён
            # как replay (L-1: подписанный payload одноразовый).
            login = _sign(
                "12345:abc",
                {"id": 100, "first_name": "A", "auth_date": int(time.time()) - 30},
            )
            client2.post("/api/auth/telegram", json=login)
            resp = client2.get(f"/api/sessions/{session_uuid}/messages")
        assert resp.status_code == 200
        history = resp.json()
        # At minimum: user_message, one text streaming_update, finished
        history_types = [h["type"] for h in history]
        assert "user_message" in history_types
        assert "finished" in history_types
        text_rows = [
            h for h in history
            if h["type"] == "streaming_update" and h["kind"] == "text"
        ]
        assert text_rows
        assert "Hi!" in "".join(h["content"] for h in text_rows)
    finally:
        fake_claude.close()
        sm.close_sync()
        sm._engine.sync_engine.dispose()
