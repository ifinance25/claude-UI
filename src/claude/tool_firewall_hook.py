"""CLI PreToolUse-hook firewall (H-1) для CLI/PTY-путей Claude.

Claude Code вызывает этот скрипт ПЕРЕД каждым инструментом (даже с
--dangerously-skip-permissions), передавая JSON на stdin. Если инструмент
нарушает политику confine'а (корень — из env ``VELS_CONFINE_ROOT``), печатаем
причину в stderr и выходим с кодом 2 → Claude НЕ выполняет инструмент. Иначе 0.

Самодостаточен: добавляет корень репозитория в sys.path, чтобы импортироваться
из любого cwd (Claude запускает хук с cwd проекта)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Корень репозитория (…/src/claude/tool_firewall_hook.py → parents[2]) в путь,
# чтобы `from src...` работал независимо от cwd хука.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.claude.tool_firewall import firewall_check  # noqa: E402


def main() -> int:
    confine_root = os.environ.get("VELS_CONFINE_ROOT", "").strip()
    if not confine_root:
        return 0  # confine не сконфигурирован — не вмешиваемся
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return 0  # не смогли распарсить вход — пропускаем (fail-open для UX)
    if not isinstance(payload, dict):
        return 0
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    audit = os.environ.get("VELS_FIREWALL_LOG") or None
    try:
        reason = firewall_check(tool_name, tool_input, confine_root, audit_log_path=audit)
    except Exception as exc:
        # Fail-CLOSED: внутренняя ошибка → блок (exit 2), а не падение с exit 1
        # (Claude трактует не-2 как «не блокировать»).
        sys.stderr.write(f"[vels-firewall] internal error, blocking: {exc}\n")
        return 2
    if reason:
        sys.stderr.write(f"[vels-firewall] blocked: {reason}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
