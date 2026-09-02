"""H-1: превентивный firewall инструментов Claude. firewall_check возвращает
причину блокировки (или None), переиспользуя SecurityMiddleware. Используется
как in-process PreToolUse-hook SDK и как CLI-hook-скрипт."""
import io
import json
from pathlib import Path

from src.claude import tool_firewall_hook
from src.claude.tool_firewall import firewall_check


def test_blocks_read_outside_project(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    reason = firewall_check("Read", {"file_path": str(Path.home() / ".ssh" / "id_rsa")}, str(root))
    assert reason  # заблокировано (выход за корень / чувствительный путь)


def test_blocks_sensitive_env_inside_project(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    reason = firewall_check("Read", {"file_path": str(root / ".env")}, str(root))
    assert reason  # .env — чувствительный путь даже внутри проекта


def test_blocks_destructive_bash(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    assert firewall_check("Bash", {"command": "rm -rf /"}, str(root))
    assert firewall_check("Bash", {"command": "cat /etc/shadow"}, str(root))


def test_allows_read_inside_project(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hi")
    assert firewall_check("Read", {"file_path": str(root / "a.txt")}, str(root)) is None


def test_block_unparseable_bash_command(tmp_path) -> None:
    """CRITICAL-регресс: Bash с несбалансированной кавычкой роняет shlex.split;
    раньше ValueError всплывал мимо `except SecurityViolation` → fail-open.
    Теперь нераспарсиваемая команда блокируется (fail-closed)."""
    root = tmp_path / "proj"
    root.mkdir()
    assert firewall_check("Bash", {"command": 'cat /etc/passwd "'}, str(root))
    assert firewall_check("Bash", {"command": "cat ../../../etc/shadow '"}, str(root))


def test_firewall_check_fail_closed_on_internal_error(tmp_path, monkeypatch) -> None:
    """Любая неожиданная ошибка внутри проверки → БЛОК (fail-closed), не пропуск."""
    import src.claude.tool_firewall as tf

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("boom in middleware ctor")

    monkeypatch.setattr(tf, "SecurityMiddleware", Boom)
    assert firewall_check("Read", {"file_path": str(tmp_path / "x")}, str(tmp_path))


def test_block_survives_unwritable_audit_log(tmp_path) -> None:
    """Регресс (найдено живым прод-тестом): если audit-log нельзя записать
    (нет прав/read-only ФС), блокировка ВСЁ РАВНО срабатывает. Иначе
    PermissionError из mkdir вылетал до SecurityViolation, и SDK-firewall-хук
    ловил его fail-open (except → {}) → инструмент проходил."""
    root = tmp_path / "proj"
    root.mkdir()
    blocker = tmp_path / "afile"
    blocker.write_text("x")  # это ФАЙЛ
    bad_audit = blocker / "sub" / "security.log"  # parent.mkdir упадёт (parent — файл)
    reason = firewall_check(
        "Read", {"file_path": "/etc/shadow"}, str(root), audit_log_path=bad_audit
    )
    assert reason  # заблокировано несмотря на несписываемый лог


def test_no_confine_root_allows_everything(tmp_path) -> None:
    # Без confine_root (доверенная сессия) — firewall не вмешивается.
    assert firewall_check("Read", {"file_path": "/etc/shadow"}, "") is None
    assert firewall_check("Bash", {"command": "rm -rf /"}, None) is None


# ── CLI-hook-скрипт (CLI/PTY-путь): exit 2 = block, exit 0 = allow ──


def test_cli_hook_blocks_violation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VELS_CONFINE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"tool_name":"Read","tool_input":{"file_path":"/etc/shadow"}}'),
    )
    assert tool_firewall_hook.main() == 2


def test_cli_hook_allows_inside(monkeypatch, tmp_path) -> None:
    (tmp_path / "a.txt").write_text("x")
    monkeypatch.setenv("VELS_CONFINE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "a.txt")}}
            )
        ),
    )
    assert tool_firewall_hook.main() == 0


def test_cli_hook_fail_closed_on_internal_error(monkeypatch, tmp_path) -> None:
    """CLI-хук при внутренней ошибке firewall_check → exit 2 (блок), а не падение
    с exit 1 (которое Claude трактует как «не блокировать»)."""
    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setenv("VELS_CONFINE_ROOT", str(tmp_path))
    monkeypatch.setattr(tool_firewall_hook, "firewall_check", _boom)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"tool_name":"Bash","tool_input":{"command":"x"}}')
    )
    assert tool_firewall_hook.main() == 2


def test_cli_hook_noop_without_confine(monkeypatch) -> None:
    monkeypatch.delenv("VELS_CONFINE_ROOT", raising=False)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}'),
    )
    assert tool_firewall_hook.main() == 0
