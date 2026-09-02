"""Bridge: форк сессии на первом resume deeplink-топика (H1).

Без форка веб-сессия и новый Telegram-топик резюмировали бы один общий
session_id in-place → дописывали бы один transcript-файл. ``mark_fork_pending``
+ ``_should_fork`` форкают ПЕРВЫЙ resume в изолированный session_id.
"""
from __future__ import annotations

from src.claude.bridge import ClaudeBridge


class _Opts:
    def __init__(self, **kw):
        self.kw = kw


def _bridge() -> ClaudeBridge:
    return ClaudeBridge(transport="sdk")


def test_should_fork_only_when_marked_with_base_and_no_own_sid():
    b = _bridge()
    assert b._should_fork(5, "websid") is False  # не помечен
    b.mark_fork_pending(5)
    assert b._should_fork(5, "websid") is True   # помечен + есть база + нет своего sid
    assert b._should_fork(5, None) is False      # нечего резюмировать
    assert b._should_fork(5, "") is False


def test_fork_consumed_once_own_sid_appears():
    b = _bridge()
    b.mark_fork_pending(5)
    assert b._should_fork(5, "websid") is True
    # Прогон вернул собственный sid топика → форк больше не нужен.
    b._session_ids[5] = "forked-sid"
    assert b._should_fork(5, "forked-sid") is False


def test_clear_cache_drops_fork_marker():
    b = _bridge()
    b.mark_fork_pending(7)
    b.clear_session_cache(7)
    assert b._should_fork(7, "x") is False


async def test_close_session_drops_fork_marker():
    b = _bridge()
    b.mark_fork_pending(9)
    await b.close_session(9)
    assert b._should_fork(9, "x") is False


def test_build_sdk_options_sets_fork_session_only_with_session():
    b = _bridge()
    o = b._build_sdk_options(
        options_cls=_Opts, project_path="/p", session_id="s", fork_session=True
    )
    assert o.kw.get("resume") == "s"
    assert o.kw.get("fork_session") is True

    o2 = b._build_sdk_options(
        options_cls=_Opts, project_path="/p", session_id="s", fork_session=False
    )
    assert "fork_session" not in o2.kw

    # Нечего резюмировать → ни resume, ни fork_session.
    o3 = b._build_sdk_options(
        options_cls=_Opts, project_path="/p", session_id=None, fork_session=True
    )
    assert "fork_session" not in o3.kw
    assert "resume" not in o3.kw


def test_build_cli_args_fork_flag():
    b = _bridge()
    args = b._build_cli_args(message="hi", session_id="s", fork_session=True)
    assert "--resume" in args
    assert "--fork-session" in args

    args2 = b._build_cli_args(message="hi", session_id="s", fork_session=False)
    assert "--fork-session" not in args2

    args3 = b._build_cli_args(message="hi", session_id=None, fork_session=True)
    assert "--fork-session" not in args3
