"""Tests for MessageHistoryPersister — bus → SQLite messages table."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.claude.session import SessionManager
from src.event_bus.bus import EventBus
from src.event_bus.events import (
    AgentFinished,
    AgentStarted,
    AgentStreamingUpdate,
    UserMessageReceived,
)
from src.web.message_store import MessageHistoryPersister


def _make_session_manager(tmp_dir: Path) -> SessionManager:
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    sm.create_session(topic_id=1, project_path="/tmp/p", project_name="p")
    return sm


def _fetch_rows(sm: SessionManager) -> list[sqlite3.Row]:
    conn = sm._get_connection()
    return list(conn.execute("SELECT * FROM messages ORDER BY event_id").fetchall())


@pytest.fixture
def tmp_dir():
    """Windows-safe temp dir: ignore errors during rmtree if sqlite holds files open."""
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def persister_and_sm(tmp_dir):
    sm = _make_session_manager(tmp_dir)
    bus = EventBus()
    persister = MessageHistoryPersister(
        bus=bus, session_manager=sm, text_flush_interval_ms=10
    )
    await persister.start()
    try:
        yield persister, bus, sm
    finally:
        await persister.stop()
        sm.close_sync()
        sm._engine.sync_engine.dispose()


async def test_persists_user_message(persister_and_sm):
    persister, bus, sm = persister_and_sm

    await bus.publish(
        UserMessageReceived(
            request_id="r1",
            chat_id=42,
            topic_id=1,
            project_path="/tmp/p",
            project_name="p",
            text="hi",
        )
    )
    await persister.flush()

    rows = _fetch_rows(sm)
    assert len(rows) == 1
    assert rows[0]["type"] == "user_message"
    assert rows[0]["content"] == "hi"
    assert rows[0]["topic_id"] == 1
    assert rows[0]["request_id"] == "r1"


async def test_persists_tool_use_event(persister_and_sm):
    persister, bus, sm = persister_and_sm

    await bus.publish(
        AgentStreamingUpdate(
            request_id="r1",
            chat_id=42,
            topic_id=1,
            kind="tool_use",
            content="Read",
            metadata={"path": "app.py"},
        )
    )
    await persister.flush()

    rows = _fetch_rows(sm)
    assert len(rows) == 1
    assert rows[0]["type"] == "streaming_update"
    assert rows[0]["kind"] == "tool_use"
    assert rows[0]["content"] == "Read"
    assert json.loads(rows[0]["metadata_json"]) == {"path": "app.py"}


async def test_persists_finished_event(persister_and_sm):
    persister, bus, sm = persister_and_sm

    await bus.publish(
        AgentFinished(
            request_id="r1",
            chat_id=42,
            topic_id=1,
            session_id="claude-abc",
            response_text="done",
            usage={"input_tokens": 5},
        )
    )
    await persister.flush()

    rows = _fetch_rows(sm)
    assert len(rows) == 1
    assert rows[0]["type"] == "finished"
    assert rows[0]["content"] == "done"
    meta = json.loads(rows[0]["metadata_json"])
    assert meta["session_id"] == "claude-abc"
    assert meta["usage"] == {"input_tokens": 5}


async def test_finished_persists_elapsed_ms(persister_and_sm):
    """AgentStarted→AgentFinished сохраняет время ответа (elapsed_ms) в метадату
    finished — чтобы оно ВОССТАНАВЛИВАЛОСЬ при перезагрузке истории."""
    persister, bus, sm = persister_and_sm

    await bus.publish(AgentStarted(request_id="r1", chat_id=42, topic_id=1))
    await bus.publish(
        AgentFinished(
            request_id="r1", chat_id=42, topic_id=1, session_id="s", response_text="ok"
        )
    )
    await persister.flush()

    finished = [r for r in _fetch_rows(sm) if r["type"] == "finished"]
    assert len(finished) == 1
    meta = json.loads(finished[0]["metadata_json"])
    assert isinstance(meta.get("elapsed_ms"), int)
    assert meta["elapsed_ms"] >= 0


async def test_finished_without_start_omits_elapsed(persister_and_sm):
    """Без AgentStarted (нет старта) elapsed не выдумываем — ключа нет."""
    persister, bus, sm = persister_and_sm

    await bus.publish(
        AgentFinished(request_id="r9", chat_id=42, topic_id=1, response_text="")
    )
    await persister.flush()

    finished = [r for r in _fetch_rows(sm) if r["type"] == "finished"]
    meta = json.loads(finished[0]["metadata_json"])
    assert "elapsed_ms" not in meta


async def test_finished_persists_session_id_to_sessions_table(persister_and_sm):
    """Веб-путь: AgentFinished с session_id пишет его в sessions.session_id.

    Раньше session_id оседал только в metadata сообщения → sessions.session_id
    оставался NULL: контекст веб-сессии терялся после рестарта, а deeplink
    «Продолжить в Telegram» не мог перенести сессию (читает session_id из БД).
    """
    persister, bus, sm = persister_and_sm

    assert sm.get_session(1).session_id in (None, "")

    await bus.publish(
        AgentFinished(
            request_id="r1",
            chat_id=42,
            topic_id=1,
            session_id="claude-web-123",
            response_text="ok",
        )
    )
    await persister.flush()

    assert sm.get_session(1).session_id == "claude-web-123"


async def test_finished_without_session_id_keeps_existing(persister_and_sm):
    """AgentFinished без session_id (ошибка до init) не затирает сохранённый."""
    persister, bus, sm = persister_and_sm
    sm.update_session_id(1, "existing-sid")

    await bus.publish(
        AgentFinished(
            request_id="r1", chat_id=42, topic_id=1, session_id=None, response_text=""
        )
    )
    await persister.flush()

    assert sm.get_session(1).session_id == "existing-sid"


async def test_thinking_kind_is_ephemeral_not_persisted(persister_and_sm):
    """kind="thinking" — эфемерный живой канал: в БД не пишется и не флашит
    накопленный текст-буфер раньше времени."""
    persister, bus, sm = persister_and_sm

    # Текст копится в буфере (одно сообщение, тот же request_id)…
    await bus.publish(
        AgentStreamingUpdate(request_id="r1", chat_id=42, topic_id=1, kind="text", content="AB")
    )
    # …пришла живая мысль — НЕ должна ни сохраниться, ни разорвать текст-буфер…
    await bus.publish(
        AgentStreamingUpdate(request_id="r1", chat_id=42, topic_id=1, kind="thinking", content="мысль")
    )
    await bus.publish(
        AgentStreamingUpdate(request_id="r1", chat_id=42, topic_id=1, kind="text", content="CD")
    )
    await persister.flush()

    rows = _fetch_rows(sm)
    # Ровно одна text-строка "ABCD"; thinking-строк нет.
    assert [r["kind"] for r in rows] == ["text"]
    assert rows[0]["content"] == "ABCD"


async def test_text_deltas_are_batched(persister_and_sm):
    persister, bus, sm = persister_and_sm

    # 5 rapid text events for the same request — should be 1 row after flush
    for char in "hello":
        await bus.publish(
            AgentStreamingUpdate(
                request_id="r1",
                chat_id=42,
                topic_id=1,
                kind="text",
                content=char,
            )
        )
    await persister.flush()

    rows = _fetch_rows(sm)
    assert len(rows) == 1
    assert rows[0]["kind"] == "text"
    assert rows[0]["content"] == "hello"

    # A non-text event between text events must flush the buffer
    await bus.publish(
        AgentStreamingUpdate(
            request_id="r1", chat_id=42, topic_id=1, kind="text", content="A"
        )
    )
    await bus.publish(
        AgentStreamingUpdate(
            request_id="r1", chat_id=42, topic_id=1, kind="tool_use", content="Read"
        )
    )
    await bus.publish(
        AgentStreamingUpdate(
            request_id="r1", chat_id=42, topic_id=1, kind="text", content="B"
        )
    )
    await persister.flush()

    rows = _fetch_rows(sm)
    # rows: hello, A, tool_use, B
    assert [r["kind"] for r in rows] == ["text", "text", "tool_use", "text"]
    assert [r["content"] for r in rows] == ["hello", "A", "Read", "B"]
