from src.claude.session import SessionManager
from src.web.bootstrap import ensure_admin_user
from src.web.passwords import verify_password


def test_creates_admin_when_none(tmp_path):
    m = SessionManager(storage_path=str(tmp_path / "s.db"))
    ensure_admin_user(m, login="root", password="rootpass1")
    u = m.get_user_by_username("root")
    assert u and u["is_admin"] == 1
    assert verify_password("rootpass1", u["password_hash"])


def test_idempotent_when_admin_exists(tmp_path):
    m = SessionManager(storage_path=str(tmp_path / "s.db"))
    ensure_admin_user(m, login="root", password="rootpass1")
    ensure_admin_user(m, login="root", password="changed-pw")
    assert verify_password("rootpass1", m.get_user_by_username("root")["password_hash"])


def test_noop_without_credentials(tmp_path):
    m = SessionManager(storage_path=str(tmp_path / "s.db"))
    ensure_admin_user(m, login="", password="")
    assert m.list_users() == []


# ── ensure_owner_admin: bootstrap the Telegram owner on a fresh DB ──────


def test_ensure_owner_admin_promotes_on_empty_db(tmp_path):
    m = SessionManager(storage_path=str(tmp_path / "s.db"))
    assert m.ensure_owner_admin(123) is True
    u = m.get_user_by_id(123)
    assert u and u["is_admin"] == 1
    # Telegram-style row: no local login, origin=telegram
    assert u["username"] is None
    assert u["origin"] == "telegram"


def test_ensure_owner_admin_noop_when_admin_exists(tmp_path):
    m = SessionManager(storage_path=str(tmp_path / "s.db"))
    # An admin was already set (e.g. by hand on an existing deployment).
    ensure_admin_user(m, login="root", password="rootpass1")
    assert m.ensure_owner_admin(456) is False
    # The whitelist user 456 must NOT have been promoted / created.
    assert m.get_user_by_id(456) is None


def test_ensure_owner_admin_idempotent(tmp_path):
    m = SessionManager(storage_path=str(tmp_path / "s.db"))
    # First launch promotes the owner.
    assert m.ensure_owner_admin(123) is True
    # Second launch (admin now present) is a safe no-op.
    assert m.ensure_owner_admin(123) is False
    assert m.ensure_owner_admin(789) is False
    assert m.get_user_by_id(789) is None
    # Owner stays admin and remains the only admin.
    admins = [u for u in m.list_users() if u["is_admin"]]
    assert [u["user_id"] for u in admins] == [123]


def test_ensure_owner_admin_flips_existing_nonadmin_owner(tmp_path):
    m = SessionManager(storage_path=str(tmp_path / "s.db"))
    # Owner already has a non-admin row (e.g. from a prior Telegram login /
    # verbose upsert). Bootstrap flips is_admin without clobbering the row.
    m.set_user_verbose(123, 2)
    assert m.get_user_by_id(123)["is_admin"] == 0
    assert m.ensure_owner_admin(123) is True
    u = m.get_user_by_id(123)
    assert u["is_admin"] == 1
    assert u["verbose_level"] == 2
