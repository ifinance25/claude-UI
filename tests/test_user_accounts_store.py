from src.claude.session import SessionManager


def _mgr(tmp_path):
    return SessionManager(storage_path=str(tmp_path / "s.db"))


def test_create_and_get_user_by_username(tmp_path) -> None:
    m = _mgr(tmp_path)
    uid = m.create_local_user(username="alice", password_hash="h", is_admin=False)
    row = m.get_user_by_username("alice")
    assert row is not None
    assert row["user_id"] == uid
    assert row["password_hash"] == "h"
    assert row["is_admin"] == 0
    assert row["is_active"] == 1


def test_project_access_grant_and_resolve(tmp_path) -> None:
    m = _mgr(tmp_path)
    uid = m.create_local_user(username="bob", password_hash="h", is_admin=False)
    m.set_project_access(uid, "/proj/a", "full")
    m.set_project_access(uid, "/proj/b", "readonly")
    access = {a["project_path"]: a["access_level"] for a in m.list_project_access(uid)}
    assert access == {"/proj/a": "full", "/proj/b": "readonly"}
    assert m.get_access_level(uid, "/proj/b") == "readonly"
    assert m.get_access_level(uid, "/proj/unknown") is None


def test_revoke_access(tmp_path) -> None:
    m = _mgr(tmp_path)
    uid = m.create_local_user(username="dan", password_hash="h", is_admin=False)
    m.set_project_access(uid, "/proj/a", "full")
    m.revoke_project_access(uid, "/proj/a")
    assert m.list_project_access(uid) == []


def test_deactivate_user(tmp_path) -> None:
    m = _mgr(tmp_path)
    uid = m.create_local_user(username="carol", password_hash="h", is_admin=False)
    m.set_user_active(uid, False)
    assert m.get_user_by_username("carol")["is_active"] == 0


def test_delete_user_removes_user_and_grants(tmp_path) -> None:
    m = _mgr(tmp_path)
    uid = m.create_local_user(username="erin", password_hash="h", is_admin=False)
    m.set_project_access(uid, "/proj/a", "full")
    assert m.delete_user(uid) is True
    assert m.get_user_by_username("erin") is None
    # гранты удалились по FK ON DELETE CASCADE
    assert m.list_project_access(uid) == []
    # повторное удаление — нечего удалять
    assert m.delete_user(uid) is False


def test_delete_user_purges_artifact_dismissals(tmp_path) -> None:
    """L1: artifact_dismissals не имеет FK к users → чистится явно."""
    m = _mgr(tmp_path)
    uid = m.create_local_user(username="frank", password_hash="h", is_admin=False)
    m.add_artifact_dismissal(uid, "/proj/a", "out/x.txt")
    assert m.list_artifact_dismissals(uid, "/proj/a") == ["out/x.txt"]
    assert m.delete_user(uid) is True
    # Скрытия удалённого пользователя не остаются висячими строками.
    assert m.list_artifact_dismissals(uid, "/proj/a") == []


def test_set_user_active_and_password_return_existence(tmp_path) -> None:
    """L2: мутации возвращают False для несуществующего user_id (роут → 404)."""
    m = _mgr(tmp_path)
    uid = m.create_local_user(username="gina", password_hash="h", is_admin=False)
    assert m.set_user_active(uid, False) is True
    assert m.set_user_password(uid, "h2") is True
    # Несуществующий пользователь → False (ранее молча возвращал None/200 OK).
    assert m.set_user_active(999999, False) is False
    assert m.set_user_password(999999, "h2") is False
