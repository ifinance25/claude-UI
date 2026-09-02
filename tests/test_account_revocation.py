"""is_account_active: классификация по origin (H-2), token-ревокация при смене
пароля (H-3), отзыв деактивированного whitelist-оператора (H-4).
"""
from src.web.dependencies import is_account_active


async def test_telegram_user_removed_from_whitelist_is_revoked(make_mgr) -> None:
    """H-2: TG-юзер с id > порога и строкой в users отзывается при снятии из whitelist."""
    m = make_mgr()
    tg = 7_900_000_000  # современный Telegram id, > LOCAL_USER_ID_MIN
    m.set_user_verbose(tg, 2)  # создаёт строку origin='telegram', is_active=1
    assert await is_account_active(m, {"user_id": tg}, allowed_user_ids={111, 222}) is False


async def test_telegram_user_in_whitelist_active(make_mgr) -> None:
    m = make_mgr()
    tg = 7_900_000_000
    m.set_user_verbose(tg, 2)
    assert await is_account_active(m, {"user_id": tg}, allowed_user_ids={tg}) is True


async def test_telegram_user_without_row_in_whitelist_active(make_mgr) -> None:
    m = make_mgr()
    tg = 7_900_000_000
    assert await is_account_active(m, {"user_id": tg}, allowed_user_ids={tg}) is True


async def test_local_user_active_token_version_match(make_mgr) -> None:
    m = make_mgr()
    uid = m.create_local_user(username="a", password_hash="h", is_admin=False)
    assert await is_account_active(m, {"user_id": uid, "tv": 0}, allowed_user_ids={111}) is True


async def test_local_user_password_changed_old_token_revoked(make_mgr) -> None:
    """H-3: смена пароля → старый JWT (tv=0) отозван, новый (tv=1) валиден."""
    m = make_mgr()
    uid = m.create_local_user(username="a", password_hash="h", is_admin=False)
    m.set_user_password(uid, "h2")  # token_version → 1
    assert await is_account_active(m, {"user_id": uid, "tv": 0}, allowed_user_ids={111}) is False
    assert await is_account_active(m, {"user_id": uid, "tv": 1}, allowed_user_ids={111}) is True


async def test_local_user_deactivated_revoked(make_mgr) -> None:
    m = make_mgr()
    uid = m.create_local_user(username="a", password_hash="h", is_admin=False)
    m.set_user_active(uid, False)
    assert await is_account_active(m, {"user_id": uid, "tv": 0}, allowed_user_ids={111}) is False


async def test_whitelist_user_deactivated_in_db_revoked(make_mgr) -> None:
    """H-4: деактивация TG-оператора в админке отзывает доступ даже при whitelist."""
    m = make_mgr()
    tg = 7_900_000_000
    m.set_user_verbose(tg, 2)
    m.set_user_active(tg, False)
    assert await is_account_active(m, {"user_id": tg}, allowed_user_ids={tg}) is False


async def test_no_session_manager_allows(tmp_path) -> None:
    assert await is_account_active(None, {"user_id": 123}, allowed_user_ids={123}) is True


async def test_no_whitelist_configured_telegram_allowed(make_mgr) -> None:
    """Legacy single-user: whitelist не настроен → TG-юзер не отзывается."""
    m = make_mgr()
    tg = 7_900_000_000
    m.set_user_verbose(tg, 2)
    assert await is_account_active(m, {"user_id": tg}, allowed_user_ids=None) is True
