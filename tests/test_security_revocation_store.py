"""Store-слой: монотонные id, origin-тегирование, анонимизация сессий при
удалении, token_version при смене пароля, UNIQUE username.

Закрывает: H-2 (классификация по origin), H-3 (token-ревокация при смене
пароля), H-8 (переиспользование id + наследование сессий), L-14 (UNIQUE
username). Решение босса: сессии при удалении юзера СОХРАНЯЮТСЯ, но владелец
снимается (анонимизация), чтобы новый юзер с тем же id их не наследовал.
"""
import pytest

from src.claude.session import SessionManager



def test_local_user_id_not_reused_after_deleting_max(make_mgr) -> None:
    """H-8: удаление юзера с максимальным id НЕ освобождает этот id."""
    m = make_mgr()
    u1 = m.create_local_user(username="a", password_hash="h", is_admin=False)
    u2 = m.create_local_user(username="b", password_hash="h", is_admin=False)
    assert u2 > u1
    m.delete_user(u2)  # удалили максимальный id
    u3 = m.create_local_user(username="c", password_hash="h", is_admin=False)
    assert u3 != u2  # id НЕ переиспользован
    assert u3 > u2  # монотонно растёт


def test_origin_tagging(make_mgr) -> None:
    """H-2: локальный аккаунт → origin='local', Telegram-юзер → 'telegram'."""
    m = make_mgr()
    local = m.create_local_user(username="loc", password_hash="h", is_admin=False)
    assert m.get_user_by_id(local).get("origin") == "local"
    tg_id = 7_900_000_000  # современный Telegram id > порога LOCAL_USER_ID_MIN
    m.set_user_verbose(tg_id, 2)  # первое изменение verbose создаёт строку users
    assert m.get_user_by_id(tg_id).get("origin") == "telegram"


def test_delete_user_anonymizes_sessions(make_mgr) -> None:
    """H-8: сессии удалённого юзера сохраняются, но владелец снимается."""
    m = make_mgr()
    uid = m.create_local_user(username="owner", password_hash="h", is_admin=False)
    sess = m.create_web_session("", "", uid)
    uuid = sess.session_uuid
    assert m.get_session_by_uuid(uuid, owner_chat_id=uid, touch=False) is not None
    m.delete_user(uid)
    # Сессия сохранена (не удаляем по решению босса)...
    assert m.get_session_by_uuid(uuid, owner_chat_id=None, touch=False) is not None
    # ...но больше НЕ принадлежит uid → новый юзер с тем же id не унаследует.
    assert m.get_session_by_uuid(uuid, owner_chat_id=uid, touch=False) is None


def test_set_password_bumps_token_version_and_timestamp(make_mgr) -> None:
    """H-3: смена пароля инкрементит token_version и ставит password_changed_at."""
    m = make_mgr()
    uid = m.create_local_user(username="pw", password_hash="h", is_admin=False)
    before = m.get_user_by_id(uid)
    assert before.get("token_version") == 0
    assert before.get("password_changed_at") is None
    m.set_user_password(uid, "h2")
    after = m.get_user_by_id(uid)
    assert after.get("token_version") == 1
    assert after.get("password_changed_at") is not None


def test_duplicate_username_rejected(make_mgr) -> None:
    """L-14: UNIQUE username — повтор логина не создаёт теневой аккаунт."""
    m = make_mgr()
    m.create_local_user(username="dup", password_hash="h", is_admin=False)
    with pytest.raises(ValueError):
        m.create_local_user(username="dup", password_hash="h2", is_admin=True)


def test_local_id_above_high_telegram_id(make_mgr) -> None:
    """H-8: счётчик поднимается над любым существующим id, включая высокий
    Telegram-id — новый локальный аккаунт не коллизит с TG-юзером."""
    m = make_mgr()
    m.set_user_verbose(7_900_000_000, 2)  # высокий Telegram-id появляется в users
    uid = m.create_local_user(username="a", password_hash="h", is_admin=False)
    assert uid > 7_900_000_000


def test_origin_backfill_on_legacy_db(tmp_path) -> None:
    """H-2: при открытии БД старой схемы (users без колонки origin) бэкфилл
    проставляет origin: system (id=0) / local (есть password_hash) / telegram."""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0,
            total_spent_usd REAL NOT NULL DEFAULT 0.0,
            verbose_level INTEGER NOT NULL DEFAULT 1,
            password_hash TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO users (user_id, username, password_hash) VALUES (0, 'system', NULL);
        INSERT INTO users (user_id, username, password_hash) VALUES (1000000005, 'loc', 'hash');
        INSERT INTO users (user_id, username, password_hash) VALUES (7900000000, NULL, NULL);
        """
    )
    conn.commit()
    conn.close()

    m = SessionManager(storage_path=str(db))  # запуск миграции + бэкфилл origin
    try:
        assert m.get_user_by_id(0)["origin"] == "system"
        assert m.get_user_by_id(1000000005)["origin"] == "local"
        assert m.get_user_by_id(7900000000)["origin"] == "telegram"
    finally:
        m.close_sync()
        m._engine.sync_engine.dispose()
