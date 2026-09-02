"""M-7 + L-9: список tool_use-артефактов скоупится по владельцу сессии и
ограничен LIMIT (не грузит всю историю БД и не светит чужую активность)."""
import json


def _add_artifact(m, topic_id, fp, ts="2026-01-01 00:00:00"):
    conn = m._get_connection()
    conn.execute(
        "INSERT INTO messages (topic_id, type, kind, content, metadata_json, created_at) "
        "VALUES (?, 'streaming_update', 'tool_use', '', ?, ?)",
        (topic_id, json.dumps({"name": "Write", "file_path": fp}), ts),
    )
    conn.commit()


def test_artifacts_scoped_to_owner(make_mgr) -> None:
    m = make_mgr()
    a = m.create_local_user(username="a", password_hash="h", is_admin=False)
    b = m.create_local_user(username="b", password_hash="h", is_admin=False)
    sa = m.create_web_session("/proj", "proj", a)
    sb = m.create_web_session("/proj", "proj", b)
    _add_artifact(m, sa.topic_id, "/proj/a_secret.txt")
    _add_artifact(m, sb.topic_id, "/proj/b_secret.txt")

    a_paths = {r["file_path"] for r in m.list_tool_artifact_rows(owner_chat_id=a)}
    assert "/proj/a_secret.txt" in a_paths
    assert "/proj/b_secret.txt" not in a_paths  # чужой артефакт не виден

    b_paths = {r["file_path"] for r in m.list_tool_artifact_rows(owner_chat_id=b)}
    assert b_paths == {"/proj/b_secret.txt"}


def test_artifacts_limit_keeps_most_recent_in_order(make_mgr) -> None:
    """LIMIT берёт N САМЫХ СВЕЖИХ и отдаёт хронологически (ORDER BY event_id).
    Проверяем ИМЕННО какие строки — иначе удаление ORDER BY не покраснеет."""
    m = make_mgr()
    a = m.create_local_user(username="a", password_hash="h", is_admin=False)
    sa = m.create_web_session("/proj", "proj", a)
    for i in range(10):
        _add_artifact(m, sa.topic_id, f"/proj/f{i}.txt", ts=f"2026-01-01 00:00:{i:02d}")
    rows = m.list_tool_artifact_rows(owner_chat_id=a, limit=3)
    assert [r["file_path"] for r in rows] == [
        "/proj/f7.txt", "/proj/f8.txt", "/proj/f9.txt",
    ]


def test_list_tool_artifact_rows_no_owner_backward_compatible(make_mgr) -> None:
    """Без owner_chat_id (legacy) — все строки (с LIMIT), как раньше."""
    m = make_mgr()
    a = m.create_local_user(username="a", password_hash="h", is_admin=False)
    sa = m.create_web_session("/proj", "proj", a)
    _add_artifact(m, sa.topic_id, "/proj/x.txt")
    rows = m.list_tool_artifact_rows()
    assert any(r["file_path"] == "/proj/x.txt" for r in rows)
