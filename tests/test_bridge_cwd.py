"""Bridge: эффективный cwd для no-project (C1) + понятная ошибка несуществующей
папки проекта (C2).

C1: сессия без проекта («чистый Claude») стартует в общем scratch-каталоге (том
же, что у веба) — иначе deeplink-resume не находит транскрипт (Claude хранит
сессии по cwd), и Claude бегает в каталоге приложения.
C2: несуществующая папка проекта → «папка проекта не найдена», а не маскирующее
«Claude Code CLI not found» (claude на месте, проблема в пути проекта).
"""
from __future__ import annotations

from pathlib import Path

from src.claude.bridge import ClaudeBridge, _classify_error


# ── C1: _resolve_cwd ────────────────────────────────────────────────


def test_resolve_cwd_uses_scratch_for_no_project(tmp_path):
    scratch = tmp_path / "scratch"
    b = ClaudeBridge(transport="sdk", scratch_dir=scratch)
    expected = str(scratch.resolve())
    assert b._resolve_cwd("") == expected
    assert b._resolve_cwd(".") == expected
    assert scratch.is_dir()  # создаётся лениво


def test_resolve_cwd_keeps_real_project_path(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    b = ClaudeBridge(transport="sdk", scratch_dir=tmp_path / "scratch")
    assert b._resolve_cwd(str(proj)) == str(Path(str(proj)).expanduser())


def test_resolve_cwd_without_scratch_keeps_dot():
    # Без настроенного scratch поведение прежнее — "." (back-compat).
    b = ClaudeBridge(transport="sdk")
    assert b._resolve_cwd("") == "."
    assert b._resolve_cwd(".") == "."


# ── C2: классификация ошибки несуществующего cwd ────────────────────


def test_classify_cwd_missing_gives_clear_message():
    from claude_agent_sdk import CLIConnectionError

    exc = CLIConnectionError(
        "Working directory does not exist: /home/test1/projects/Test"
    )
    etype, msg = _classify_error(exc)
    assert etype == "cwd_missing"
    assert "не найдена" in msg
    assert "/home/test1/projects/Test" in msg
    assert "CLI" not in msg  # не маскируем под «CLI не найден»


def test_classify_generic_cli_error_still_says_cli_not_found():
    from claude_agent_sdk import CLIConnectionError

    exc = CLIConnectionError("could not locate the claude binary anywhere")
    etype, msg = _classify_error(exc)
    assert etype == "unknown"
    assert "CLI not found" in msg
