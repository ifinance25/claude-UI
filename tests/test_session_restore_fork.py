"""Восстановление session_id из БД на старте бота + детект форка для deeplink.

Баг: deeplink «Продолжить в Telegram» оставляет веб-сессию и новый
Telegram-топик с ОДНИМ Claude session_id. В рамках процесса форк на первом
resume обеспечивает in-memory ``_fork_pending``, но при рестарте бота тот флаг
теряется, а общий session_id остаётся в БД → resume копии без форка дописывал
бы общий transcript. ``plan_sdk_session_restore`` детектирует коллизию по самой
БД: если session_id делят несколько топиков, копию (положительный topic_id —
Telegram-топик; веб-сессии всегда в отрицательном диапазоне) помечаем на форк,
а не резюмируем in-place.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.bot.core import plan_sdk_session_restore


def _s(topic_id: int, session_id: str | None):
    return SimpleNamespace(topic_id=topic_id, session_id=session_id)


def test_shared_session_id_forks_telegram_copy_not_web_original():
    # Веб-сессия (отриц. topic) и Telegram-топик (полож. topic) делят один sid.
    sessions = [_s(-1, "S"), _s(5, "S")]
    restore_map, fork_pending = plan_sdk_session_restore(sessions, existing_topic_ids=set())
    # Веб-оригинал резюмируется in-place; Telegram-копия форкается на 1-м resume.
    assert restore_map == {-1: "S"}
    assert fork_pending == [5]


def test_no_collision_restores_all_in_place():
    sessions = [_s(-1, "A"), _s(5, "B")]
    restore_map, fork_pending = plan_sdk_session_restore(sessions, existing_topic_ids=set())
    assert restore_map == {-1: "A", 5: "B"}
    assert fork_pending == []


def test_existing_topic_ids_are_skipped():
    # Уже восстановленные/живые в памяти топики не трогаем.
    sessions = [_s(-1, "S"), _s(5, "S")]
    restore_map, fork_pending = plan_sdk_session_restore(sessions, existing_topic_ids={5})
    assert restore_map == {-1: "S"}
    assert fork_pending == []  # 5 уже в памяти → пропущен


def test_sessions_without_session_id_skipped():
    sessions = [_s(-1, None), _s(5, "")]
    restore_map, fork_pending = plan_sdk_session_restore(sessions, existing_topic_ids=set())
    assert restore_map == {}
    assert fork_pending == []


def test_two_telegram_copies_of_one_web_session_both_fork():
    sessions = [_s(-1, "S"), _s(5, "S"), _s(6, "S")]
    restore_map, fork_pending = plan_sdk_session_restore(sessions, existing_topic_ids=set())
    assert restore_map == {-1: "S"}
    assert sorted(fork_pending) == [5, 6]


def test_orphaned_telegram_copy_without_web_resumes_in_place():
    # Веб-сессию удалили — коллизии больше нет, Telegram-топик резюмирует sid
    # in-place (он унаследовал контекст, конфликта дозаписи уже нет).
    sessions = [_s(5, "S")]
    restore_map, fork_pending = plan_sdk_session_restore(sessions, existing_topic_ids=set())
    assert restore_map == {5: "S"}
    assert fork_pending == []
