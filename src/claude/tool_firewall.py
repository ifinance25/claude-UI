"""Превентивный firewall инструментов Claude (H-1).

С ``permission_mode=bypassPermissions`` единственный рубеж, способный
ОСТАНОВИТЬ инструмент ДО выполнения, — это PreToolUse-hook (он срабатывает
даже при --dangerously-skip-permissions). Эта логика подключается двумя путями:

* как in-process async-hook в опциях claude-agent-sdk (основной веб-путь),
* как отдельный CLI-hook-скрипт (``python tool_firewall_hook.py``) для
  CLI/PTY-путей через ``--settings``.

Применяется ТОЛЬКО к «несвободным» сессиям (локальный/непривилегированный юзер
с доступом к конкретному проекту): такие confine'ятся в корень проекта и им
запрещён доступ к секретам/деструктивные команды. Для admin/whitelist —
firewall не навешивается (confine_root=None → no-op).
"""
from __future__ import annotations

from pathlib import Path

from src.bot.middleware.security import SecurityMiddleware, SecurityViolation


def firewall_check(
    tool_name: str,
    tool_input: dict | None,
    confine_root: str | Path | None,
    *,
    audit_log_path: str | Path | None = None,
) -> str | None:
    """Вернуть причину блокировки инструмента или ``None`` (разрешено).

    ``confine_root`` пуст/None → доверенная сессия, firewall не вмешивается.
    Иначе проверяем через ``SecurityMiddleware.validate_tool_use``: выход за
    пределы корня (path-traversal), доступ к чувствительным файлам
    (.env/.ssh/ключи) и деструктивные bash-команды (rm -rf, shutdown, …).
    """
    if not confine_root:
        return None
    if not tool_name:
        return None
    try:
        root = Path(confine_root)
        audit = audit_log_path or (root / ".claude" / "security.log")
        mw = SecurityMiddleware(allowed_roots=[root], audit_log_path=audit)
        mw.validate_tool_use(
            tool_name=tool_name,
            tool_input=tool_input or {},
            project_root=str(root),
        )
    except SecurityViolation as exc:
        return f"{exc} (category={exc.category})"
    except Exception as exc:
        # Fail-closed: ЛЮБАЯ неожиданная ошибка (валидация, создание middleware,
        # резолв путей) → блок, а не пропуск. Иначе рантайм-исключение из внешней
        # либы = обход firewall'а.
        return f"firewall internal error (blocked fail-closed): {exc}"
    return None
