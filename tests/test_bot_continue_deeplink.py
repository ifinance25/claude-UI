"""Бот: deeplink /start continue_<uuid> — создание топика + перенос session_id."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.handlers import commands
from src.bot.handlers.commands import _continue_in_topic, parse_continue_payload
from src.claude.session import SessionManager


def test_parse_continue_payload():
    assert parse_continue_payload("continue_abc-123") == "abc-123"
    assert parse_continue_payload("continue_") is None
    assert parse_continue_payload("") is None
    assert parse_continue_payload(None) is None
    assert parse_continue_payload("garbage") is None


def _bot_with_topic(topic_id=555):
    bot = AsyncMock()
    bot.create_forum_topic.return_value = SimpleNamespace(message_thread_id=topic_id)
    return bot


async def test_continue_creates_topic_and_resumes():
    bot = _bot_with_topic(555)
    bridge = MagicMock()
    sm = AsyncMock()
    sm.async_get_session_by_uuid.return_value = SimpleNamespace(
        project_path="/p", project_name="alpha", session_id="claude-sid-1", chat_id=100,
    )
    await _continue_in_topic(
        bot=bot, session_manager=sm, claude_bridge=bridge,
        uuid="u1", from_user_id=100, chat_id=100,
    )
    sm.async_get_session_by_uuid.assert_awaited_once_with("u1", owner_chat_id=100, touch=False)
    bot.create_forum_topic.assert_awaited_once()
    sm.async_create_session.assert_awaited_once_with(555, "/p", "alpha", 100)
    sm.async_update_session_id.assert_awaited_once_with(555, "claude-sid-1")
    # H1: новый топик помечается на форк — чтобы веб-сессия и топик не делили
    # один transcript при resume общего session_id.
    bridge.mark_fork_pending.assert_called_once_with(555)
    assert bot.send_message.await_count == 1  # сообщение в топик


async def test_continue_without_session_id_no_resume():
    bot = _bot_with_topic(556)
    bridge = MagicMock()
    sm = AsyncMock()
    sm.async_get_session_by_uuid.return_value = SimpleNamespace(
        project_path="/p", project_name="alpha", session_id=None, chat_id=100,
    )
    await _continue_in_topic(
        bot=bot, session_manager=sm, claude_bridge=bridge,
        uuid="u1", from_user_id=100, chat_id=100,
    )
    sm.async_create_session.assert_awaited_once_with(556, "/p", "alpha", 100)
    sm.async_update_session_id.assert_not_awaited()
    # Нет session_id → нечего форкать.
    bridge.mark_fork_pending.assert_not_called()


async def test_continue_foreign_or_missing_session_denied():
    bot = _bot_with_topic()
    bridge = MagicMock()
    sm = AsyncMock()
    sm.async_get_session_by_uuid.return_value = None  # чужая/нет → owner-check вернул None
    await _continue_in_topic(
        bot=bot, session_manager=sm, claude_bridge=bridge,
        uuid="u1", from_user_id=999, chat_id=999,
    )
    bot.create_forum_topic.assert_not_awaited()
    sm.async_create_session.assert_not_awaited()
    bridge.mark_fork_pending.assert_not_called()
    assert bot.send_message.await_count == 1  # «не найдена/недоступна»


async def test_continue_topic_creation_failure_fallback():
    bot = AsyncMock()
    bot.create_forum_topic.side_effect = RuntimeError("forum disabled")
    bridge = MagicMock()
    sm = AsyncMock()
    sm.async_get_session_by_uuid.return_value = SimpleNamespace(
        project_path="/p", project_name="alpha", session_id="s", chat_id=100,
    )
    await _continue_in_topic(
        bot=bot, session_manager=sm, claude_bridge=bridge,
        uuid="u1", from_user_id=100, chat_id=100,
    )
    assert bot.send_message.await_count == 1  # fallback в основной чат
    sm.async_create_session.assert_not_awaited()
    bridge.mark_fork_pending.assert_not_called()


async def test_continue_rolls_back_orphan_topic_on_post_create_failure():
    """L3: сбой ПОСЛЕ create_forum_topic → откат осиротевшего топика + точное сообщение."""
    bot = _bot_with_topic(777)
    bridge = MagicMock()
    sm = AsyncMock()
    sm.async_get_session_by_uuid.return_value = SimpleNamespace(
        project_path="/p", project_name="alpha", session_id="s", chat_id=100,
    )
    sm.async_create_session.side_effect = RuntimeError("db locked")
    await _continue_in_topic(
        bot=bot, session_manager=sm, claude_bridge=bridge,
        uuid="u1", from_user_id=100, chat_id=100,
    )
    bot.create_forum_topic.assert_awaited_once()
    bot.delete_forum_topic.assert_awaited_once()  # осиротевший топик удалён
    sm.async_close_session.assert_awaited_once_with(777)
    # Сообщение в ОСНОВНОЙ чат (без message_thread_id), НЕ «не удалось создать топик».
    last = bot.send_message.await_args
    assert "message_thread_id" not in last.kwargs
    assert "восстановить" in last.kwargs["text"].lower()


# --- cmd_start: роутинг deeplink-payload (T1/T2) ---


async def test_cmd_start_routes_deeplink_payload(monkeypatch):
    """T1: cmd_start извлекает uuid и пробрасывает from_user.id/chat.id/bridge."""
    spy = AsyncMock()
    monkeypatch.setattr(commands, "_continue_in_topic", spy)
    sentinel_sm = object()
    sentinel_bridge = object()
    monkeypatch.setattr(commands.router, "session_manager", sentinel_sm, raising=False)
    monkeypatch.setattr(commands.router, "claude_bridge", sentinel_bridge, raising=False)
    msg = SimpleNamespace(
        bot=AsyncMock(),
        from_user=SimpleNamespace(id=100, username="u"),
        chat=SimpleNamespace(id=100),
        answer=AsyncMock(),
    )
    await commands.cmd_start(msg, SimpleNamespace(args="continue_u1"))
    spy.assert_awaited_once()
    kw = spy.await_args.kwargs
    assert kw["uuid"] == "u1"
    assert kw["from_user_id"] == 100
    assert kw["chat_id"] == 100
    assert kw["session_manager"] is sentinel_sm
    assert kw["claude_bridge"] is sentinel_bridge
    msg.answer.assert_not_called()  # онбординг не отправлялся


async def test_cmd_start_deeplink_no_from_user_passes_zero(monkeypatch):
    """T2: from_user=None → from_user_id=0 (sentinel, при котором owner-check отклоняет)."""
    spy = AsyncMock()
    monkeypatch.setattr(commands, "_continue_in_topic", spy)
    monkeypatch.setattr(commands.router, "session_manager", object(), raising=False)
    monkeypatch.setattr(commands.router, "claude_bridge", object(), raising=False)
    msg = SimpleNamespace(
        bot=AsyncMock(), from_user=None, chat=SimpleNamespace(id=100), answer=AsyncMock(),
    )
    await commands.cmd_start(msg, SimpleNamespace(args="continue_u1"))
    assert spy.await_args.kwargs["from_user_id"] == 0


async def test_cmd_start_non_deeplink_runs_onboarding(monkeypatch):
    """Обычный /start (без payload) не вызывает _continue_in_topic."""
    spy = AsyncMock()
    monkeypatch.setattr(commands, "_continue_in_topic", spy)
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=100, username="u"),
        chat=SimpleNamespace(id=100),
        answer=AsyncMock(),
    )
    await commands.cmd_start(msg, SimpleNamespace(args=None))
    spy.assert_not_awaited()
    msg.answer.assert_awaited_once()


def test_owner_check_zero_rejects_real_session(tmp_path):
    """T2 (инвариант): owner_chat_id=0 отклоняет реальную сессию; None — отключает проверку."""
    sm = SessionManager(storage_path=tmp_path / "s.db")
    try:
        sm.create_session(topic_id=-1, project_path="/p", project_name="a", chat_id=100)
        sess = sm.get_all_sessions(owner_chat_id=100)[0]
        # from_user is None → fallback 0 → не должно вернуть НИ одну сессию (chat_id != 0).
        assert sm.get_session_by_uuid(sess.session_uuid, owner_chat_id=0, touch=False) is None
        # Правильный владелец — возвращает.
        assert sm.get_session_by_uuid(sess.session_uuid, owner_chat_id=100, touch=False) is not None
        # owner_chat_id=None — owner-check ОТКЛЮЧЁН (поэтому 0, а не None, — load-bearing).
        assert sm.get_session_by_uuid(sess.session_uuid, owner_chat_id=None, touch=False) is not None
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
