"""Чистый текстовый слой нативных (TUI-only) слеш-команд.

Claude CLI не поддерживает ``/config`` ``/model`` ``/mcp`` ``/permissions``
через ``-p``. Бот перехватывал их сам; веб — нет (уходили в claude → «isn't
available in this environment»). Здесь — общие функции, возвращающие ГОТОВЫЙ
ТЕКСТ (Markdown, без Telegram-HTML и без aiogram), пригодный и для веба.
"""
from __future__ import annotations

from src.claude.claude_settings import read_claude_settings
from src.claude.models import DEFAULT_MODEL, model_hint, model_label, normalize_model_id

TUI_COMMANDS = {"/config", "/mcp", "/model", "/permissions"}

# ВАЖНО (безопасность): веб-перехват нативных команд доступен ЛЮБОМУ
# аутентифицированному пользователю (в т.ч. readonly), поэтому вывод НЕ должен
# раскрывать секреты из ~/.claude/settings.json — ни сырой json (в нём `env` с
# прокси-кредами, apiKeyHelper, MCP-заголовки/токены), ни deny-паттерны
# firewall (раскрывают защиту). Показываем только безопасное подмножество.


def render_config_report() -> str:
    data = read_claude_settings()
    if not data:
        return "⚙️ Конфиг пуст (~/.claude/settings.json)."
    model = data.get("model", "default")
    mode = data.get("permissions", {}).get("defaultMode", "default")
    lines = [
        "⚙️ **Claude Code Config**",
        "",
        f"**Модель:** `{model}`",
        f"**Режим разрешений:** `{mode}`",
    ]
    mcp = data.get("mcpServers")
    if isinstance(mcp, dict) and mcp:
        # только ИМЕНА серверов (не команды/токены/заголовки)
        lines.append(f"**MCP-серверов:** {len(mcp)} ({', '.join(sorted(mcp))})")
    return "\n".join(lines)


def render_permissions_report() -> str:
    data = read_claude_settings()
    perms = data.get("permissions", {}) if isinstance(data, dict) else {}
    mode = perms.get("defaultMode", "default")
    allow = perms.get("allow") or []
    deny = perms.get("deny") or []
    # Только режим и СЧЁТЧИКИ правил — сами паттерны не раскрываем.
    return (
        "🔐 **Permissions**\n\n"
        f"**Режим:** `{mode}`\n"
        f"**Правил:** allow {len(allow)}, deny {len(deny)}"
    )


def render_model_report() -> str:
    data = read_claude_settings()
    current = normalize_model_id(str(data.get("model") or DEFAULT_MODEL))
    hint = model_hint(current)
    return (
        "🤖 **Модель Claude**\n\n"
        f"Текущая: **{model_label(current)}**\n"
        f"{hint}\n\n"
        "Сменить модель можно кнопочным селектором модели в интерфейсе."
    )


def render_mcp_report() -> str:
    # L-5: раньше /config показывал mcpServers из settings.json, а /mcp — только
    # ссылку на «Подключения» → противоречивая картина. Сводим в один правдивый
    # отчёт. Показываем ТОЛЬКО имена серверов (без команд/токенов/заголовков) —
    # вывод доступен любому аутентиф. юзеру (см. заметку о безопасности вверху).
    data = read_claude_settings()
    native = data.get("mcpServers") if isinstance(data, dict) else None
    lines = ["🔌 **MCP / Подключения**", ""]
    if isinstance(native, dict) and native:
        lines.append(
            f"**Нативные MCP (settings.json):** {len(native)} "
            f"({', '.join(sorted(native))})"
        )
        lines.append(
            "_Project-scope MCP (`.mcp.json`) в headless-режиме требуют "
            "`enableAllProjectMcpServers: true` в settings.json сервис-юзера — "
            "установщик выставляет это для owner-сессий. Статус подключения "
            "по каждому серверу пишется в лог при старте (см. `mcp_init`)._"
        )
        lines.append("")
    lines.append(
        "Персональные подключения сервисов (per-user) — в настройках, раздел "
        "«Подключения» (иконка настроек → «Подключения»). Участникам проекта "
        "MCP выдаются именно через «Подключения», а не нативно."
    )
    return "\n".join(lines)


def render_native_command(text: str) -> str | None:
    """Если text — нативная команда, вернуть готовый текст-ответ; иначе None."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    cmd = stripped.split()[0].lower()
    if cmd not in TUI_COMMANDS:
        return None
    return {
        "/config": render_config_report,
        "/permissions": render_permissions_report,
        "/model": render_model_report,
        "/mcp": render_mcp_report,
    }[cmd]()
