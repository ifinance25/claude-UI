"""Regression tests for Code Review №3 fixes (feat/web-ui).

Each test pins one finding so a future refactor that reintroduces the bug
fails loudly. Grouped by CR3-number from CODE_REVIEW_FINDINGS.md.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.claude.session import SessionManager
from src.event_bus.bus import EventBus, SubscriberPriority
from src.event_bus.events import UserMessageReceived


@pytest.fixture
def sm(tmp_path: Path):
    manager = SessionManager(storage_path=tmp_path / "sessions.db")
    try:
        yield manager
    finally:
        manager.close_sync()
        manager._engine.sync_engine.dispose()


def _raw_last_activity(manager: SessionManager, topic_id: int) -> str:
    with manager._conn_lock:
        row = manager._get_connection().execute(
            "SELECT last_activity FROM sessions WHERE topic_id = ?", (topic_id,)
        ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# CR3-1 — atomic web-session creation (no topic_id collision / owner overwrite)
# ---------------------------------------------------------------------------


def test_create_web_session_allocates_unique_negative_ids(sm: SessionManager):
    s1 = sm.create_web_session("/p", "p", chat_id=1)
    s2 = sm.create_web_session("/p", "p", chat_id=1)
    assert s1.topic_id < 0 and s2.topic_id < 0
    assert s1.topic_id != s2.topic_id
    # The returned UUID must actually be the persisted one (ON CONFLICT
    # COALESCE used to return an in-memory uuid that was never stored).
    assert sm.get_session_by_uuid(s2.session_uuid, owner_chat_id=1) is not None
    assert sm.get_session_by_uuid(s1.session_uuid, owner_chat_id=1) is not None


def test_create_web_session_concurrent_no_collision(sm: SessionManager):
    n = 8
    barrier = threading.Barrier(n)
    results: list = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        s = sm.create_web_session("/p", "p", chat_id=1)
        with lock:
            results.append(s)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    topic_ids = {r.topic_id for r in results}
    assert len(topic_ids) == n, "concurrent creates collided on topic_id"
    # Every returned session must be retrievable by its own UUID and keep
    # its own owner — i.e. no silent ON CONFLICT owner overwrite.
    for r in results:
        fetched = sm.get_session_by_uuid(r.session_uuid, owner_chat_id=1)
        assert fetched is not None
        assert fetched.topic_id == r.topic_id


# ---------------------------------------------------------------------------
# CR3-13 — web verbose default propagates to new sessions
# ---------------------------------------------------------------------------


def test_create_web_session_inherits_user_default_verbose(sm: SessionManager):
    sm.set_user_verbose(1, 3)
    s = sm.create_web_session("/p", "p", chat_id=1)
    assert s.verbose_level == 3
    assert sm.get_session(s.topic_id).verbose_level == 3


def test_create_web_session_defaults_verbose_to_one_without_user(sm: SessionManager):
    s = sm.create_web_session("/p", "p", chat_id=999)
    assert s.verbose_level == 1


# ---------------------------------------------------------------------------
# CR3-7 — get_session_by_uuid must not write last_activity on read paths
# ---------------------------------------------------------------------------


def test_get_session_by_uuid_touch_false_keeps_last_activity(sm: SessionManager):
    s = sm.create_web_session("/p", "p", chat_id=1)
    la0 = _raw_last_activity(sm, s.topic_id)
    time.sleep(0.02)
    got = sm.get_session_by_uuid(s.session_uuid, owner_chat_id=1, touch=False)
    assert got is not None
    assert _raw_last_activity(sm, s.topic_id) == la0, "touch=False must not write"


def test_get_session_by_uuid_touch_true_updates_last_activity(sm: SessionManager):
    s = sm.create_web_session("/p", "p", chat_id=1)
    la0 = _raw_last_activity(sm, s.topic_id)
    time.sleep(0.02)
    sm.get_session_by_uuid(s.session_uuid, owner_chat_id=1)  # default touch=True
    assert _raw_last_activity(sm, s.topic_id) != la0


# ---------------------------------------------------------------------------
# CR3-5 — critical bus subscriber failure must not be swallowed
# ---------------------------------------------------------------------------


def _user_event() -> UserMessageReceived:
    return UserMessageReceived(
        request_id="r", chat_id=1, topic_id=1, project_path="/tmp", text="hi"
    )


async def test_critical_subscriber_failure_aborts_publish():
    bus = EventBus()
    ran: list[str] = []

    async def failing(_e):
        raise RuntimeError("db down")

    async def downstream(_e):
        ran.append("downstream")

    bus.subscribe(
        UserMessageReceived, failing, priority=SubscriberPriority.NORMAL, critical=True
    )
    bus.subscribe(UserMessageReceived, downstream, priority=SubscriberPriority.LOW)

    with pytest.raises(RuntimeError):
        await bus.publish(_user_event())
    # The LOW-priority consumer (Claude bridge) must NOT run when the
    # critical persister failed.
    assert ran == []


async def test_noncritical_subscriber_failure_is_isolated():
    bus = EventBus()
    ran: list[str] = []

    async def flaky(_e):
        raise RuntimeError("transient")

    async def downstream(_e):
        ran.append("downstream")

    bus.subscribe(UserMessageReceived, flaky, priority=SubscriberPriority.HIGHEST)
    bus.subscribe(UserMessageReceived, downstream, priority=SubscriberPriority.LOW)

    # No raise — non-critical failure stays isolated, downstream still runs.
    await bus.publish(_user_event())
    assert ran == ["downstream"]


# ---------------------------------------------------------------------------
# CR3-18 — shared path-containment helper
# ---------------------------------------------------------------------------


def test_is_path_within_root(tmp_path: Path):
    from src.utils.url_safety import is_path_within_root

    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    assert is_path_within_root(root, root / "sub" / "f.txt")
    assert is_path_within_root(root, root)
    assert not is_path_within_root(root, tmp_path / "outside.txt")
    assert not is_path_within_root(root, root / ".." / "escape.txt")


# ---------------------------------------------------------------------------
# CR3-22 — attachment cleanup is wired and traversal-safe
# ---------------------------------------------------------------------------


def _attachment(source_path: str, kind: str = "image"):
    from src.claude.bridge import ClaudeImageAttachment

    return ClaudeImageAttachment(
        source_path=source_path,
        file_name=Path(source_path).name,
        mime_type="image/png" if kind == "image" else "text/plain",
        base64_data="",
        kind=kind,
    )


def test_cleanup_attachments_removes_uploaded_file(tmp_path: Path):
    from src.claude.bridge import ClaudeBridge

    uploads = tmp_path / ".claude" / "uploads"
    uploads.mkdir(parents=True)
    f = uploads / "shot.png"
    f.write_bytes(b"data")
    att = _attachment(".claude/uploads/shot.png")
    # _cleanup_attachments doesn't touch self — call unbound with self=None.
    ClaudeBridge._cleanup_attachments(None, [att], tmp_path)
    assert not f.exists(), "uploaded file should be removed after processing"


def test_cleanup_attachments_blocks_traversal(tmp_path: Path):
    from src.claude.bridge import ClaudeBridge

    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("keep me")
    att = _attachment("../secret.txt", kind="text")
    ClaudeBridge._cleanup_attachments(None, [att], project)
    assert outside.exists(), "traversal path outside project must NOT be deleted"
