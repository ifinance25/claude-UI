"""Registry of bot commands and Claude Code slash commands.

Dynamically fetches available slash commands from Claude Code CLI
(via the ``init`` event in stream-json output) and registers them with
Telegram Bot API so they appear in the user's ``/`` menu.

Namespaced commands (e.g. ``superpowers:brainstorm``) are registered
with ``_`` instead of ``:`` (Telegram doesn't allow colons) and converted
back before sending to Claude CLI.

Commands are refreshed lazily — only when a user sends a message AND
the cache is older than 1 hour. Zero load when idle.
"""
from __future__ import annotations

import asyncio
import json
import time

import structlog
from aiogram import Bot
from aiogram.types import BotCommand

logger = structlog.get_logger()

# How often to re-fetch commands from CLI (seconds)
_REFRESH_INTERVAL = 60 * 60  # 1 hour

# Bot's own commands (handled by our handlers, always present)
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "Запустить бота"),
    ("projects", "Список проектов"),
    ("status", "Статус сессии"),
    ("settings", "Настройки бота"),
    ("usage", "Статистика токенов"),
    ("skills", "Доступные команды"),
    ("verbose", "Уровень детализации"),
    ("stop", "Прервать генерацию"),
    ("close", "Закрыть сессию"),
    ("auth", "Авторизация Claude"),
    ("weblogin", "Ссылка для входа в веб"),
    ("help", "Справка"),
]

# Human-friendly descriptions for known Claude commands.
_KNOWN_DESCRIPTIONS: dict[str, str] = {
    "clear": "Очистить историю диалога",
    "compact": "Сжать диалог для экономии контекста",
    "config": "Настройки Claude Code",
    "context": "Использование контекстного окна",
    "cost": "Статистика токенов и стоимости",
    "diff": "Просмотр незакоммиченных изменений",
    "export": "Экспорт диалога в текст",
    "fast": "Переключить быстрый режим",
    "init": "Инициализировать CLAUDE.md",
    "memory": "Управление CLAUDE.md и памятью",
    "model": "Выбор модели AI",
    "mcp": "Управление MCP-серверами",
    "permissions": "Просмотр/изменение разрешений",
    "review": "Ревью pull request",
    "commit": "Создать git commit",
    "login": "Войти в аккаунт Anthropic",
    "logout": "Выйти из аккаунта",
    "doctor": "Диагностика установки",
    "rename": "Переименовать сессию",
    "fork": "Создать копию диалога",
    "plan": "Войти в режим планирования",
    "output_style": "Стиль вывода ответов",
    "debug": "Режим отладки",
    "simplify": "Ревью и упрощение кода",
    "batch": "Пакетная обработка файлов",
    "pdf": "Работа с PDF файлами",
    "security-review": "Проверка безопасности кода",
    "release-notes": "Генерация release notes",
    "pr-comments": "Комментарии к PR",
    "insights": "Анализ и инсайты",
    "extra-usage": "Расширенная статистика",
}

# Commands to skip (internal/irrelevant for Telegram)
_SKIP_COMMANDS: set[str] = {"heapdump"}

# Bot command names (to avoid duplicates)
_BOT_COMMAND_NAMES: set[str] = {cmd for cmd, _ in BOT_COMMANDS}

# ── Namespace mapping ──────────────────────────────────────────
# Telegram doesn't allow ":" in slash commands.
# We register "superpowers:brainstorm" as "superpowers_brainstorm"
# and convert back when sending to Claude CLI.
#
# _tg_to_cli:  "superpowers_brainstorm" → "superpowers:brainstorm"

_tg_to_cli: dict[str, str] = {}

# Cache state
_cached_cli_commands: list[str] = []
_last_fetch_time: float = 0.0


def _to_telegram_name(cli_cmd: str) -> str:
    """Convert CLI command name to valid Telegram command.

    "superpowers:brainstorm" → "superpowers_brainstorm"
    Telegram allows: a-z, 0-9, _ (1-32 chars, lowercase)
    """
    return cli_cmd.replace(":", "_").replace("-", "_").lower()[:32]


def resolve_command(tg_cmd: str) -> str:
    """Convert a Telegram command back to CLI format.

    Called by message handlers before sending to Claude CLI.
    "/superpowers_brainstorm" → "/superpowers:brainstorm"
    """
    # Strip leading /
    bare = tg_cmd.lstrip("/")
    # Look up in mapping
    cli_name = _tg_to_cli.get(bare)
    if cli_name:
        return f"/{cli_name}"
    return tg_cmd


async def _fetch_cli_slash_commands(timeout: float = 15.0) -> list[str]:
    """Run ``claude -p "hi" --output-format stream-json --verbose --max-turns 1``
    and extract ``slash_commands`` from the ``init`` event.

    ``--verbose`` is required: the CLI rejects ``--print`` combined with
    ``--output-format=stream-json`` unless ``--verbose`` is present.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "hi",
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", "1",
            "--dangerously-skip-permissions",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "system" and data.get("subtype") == "init":
                commands = data.get("slash_commands", [])
                if commands:
                    logger.info("cli_slash_commands_fetched", count=len(commands))
                    return commands
    except asyncio.TimeoutError:
        logger.warning("cli_slash_commands_timeout", timeout=timeout)
    except FileNotFoundError:
        logger.warning("cli_slash_commands_not_found", hint="claude CLI not in PATH")
    except Exception as e:
        logger.warning("cli_slash_commands_error", error=str(e))

    return []


def _make_description(cmd: str) -> str:
    """Generate a description for an unknown command."""
    # "superpowers:brainstorm" → "Superpowers · Brainstorm"
    if ":" in cmd:
        plugin, name = cmd.split(":", 1)
        nice_name = name.replace("-", " ").replace("_", " ").capitalize()
        return f"{plugin.capitalize()} · {nice_name}"
    return cmd.replace("-", " ").replace("_", " ").capitalize()


def _build_bot_commands(cli_commands: list[str]) -> list[BotCommand]:
    """Build the full command list from bot + CLI commands."""
    global _tg_to_cli

    commands: list[BotCommand] = []
    new_mapping: dict[str, str] = {}

    for cmd, desc in BOT_COMMANDS:
        commands.append(BotCommand(command=cmd, description=desc))

    for cmd in cli_commands:
        if cmd in _SKIP_COMMANDS:
            continue

        tg_name = _to_telegram_name(cmd)

        if tg_name in _BOT_COMMAND_NAMES:
            continue

        # Track mapping for namespaced commands
        if ":" in cmd or "-" in cmd:
            new_mapping[tg_name] = cmd

        desc = _KNOWN_DESCRIPTIONS.get(cmd, _make_description(cmd))
        commands.append(BotCommand(command=tg_name, description=desc))

    _tg_to_cli = new_mapping

    # Telegram limit: 100 commands max
    return commands[:100]


async def _do_sync(bot: Bot) -> None:
    """Fetch commands from CLI and push to Telegram if changed."""
    global _cached_cli_commands, _last_fetch_time

    new_commands = await _fetch_cli_slash_commands()
    _last_fetch_time = time.monotonic()

    if not new_commands:
        if not _cached_cli_commands:
            logger.info("using_static_command_list")
            new_commands = list(_KNOWN_DESCRIPTIONS.keys())
        else:
            return

    if new_commands == _cached_cli_commands:
        logger.debug("commands_unchanged", count=len(new_commands))
        return

    _cached_cli_commands = new_commands
    bot_commands = _build_bot_commands(new_commands)

    try:
        await bot.set_my_commands(bot_commands)
        logger.info(
            "commands_synced",
            count=len(bot_commands),
            namespaced=len(_tg_to_cli),
        )
    except Exception as e:
        logger.error("commands_sync_failed", error=str(e))


async def sync_commands(bot: Bot) -> None:
    """Sync commands once at bot startup."""
    await _do_sync(bot)


async def refresh_commands_if_needed(bot: Bot) -> None:
    """Refresh commands only when a user is active AND cache is stale.

    Called from message handlers. No background loops — zero load
    when the bot is idle.
    """
    elapsed = time.monotonic() - _last_fetch_time
    if elapsed < _REFRESH_INTERVAL:
        return

    asyncio.create_task(_do_sync(bot))


# ── Web UI: динамический список команд под проект ──────────────
# Переиспользуем тот же механизм, что и Telegram (init-событие CLI),
# но с cwd проекта и кэшем по project_path, чтобы веб показывал столько
# же команд, сколько код-код/бот, а не статичный список.

_cli_cmd_cache: dict[str, tuple[float, list[str]]] = {}
_CLI_CMD_TTL = 60 * 60  # 1 час


async def fetch_cli_commands_for_project(project_path: str | None) -> list[str]:
    """slash_commands из init-события Claude, запущенного в cwd проекта.
    Кэш на час по project_path. Пустой список при ошибке/таймауте."""
    key = project_path or ""
    now = time.monotonic()
    hit = _cli_cmd_cache.get(key)
    if hit and now - hit[0] < _CLI_CMD_TTL:
        return hit[1]
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "hi",
            "--output-format", "stream-json",
            "--max-turns", "1",
            "--dangerously-skip-permissions",
            cwd=project_path or None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "system" and data.get("subtype") == "init":
                cmds = data.get("slash_commands", []) or []
                _cli_cmd_cache[key] = (now, cmds)
                return cmds
    except asyncio.TimeoutError:
        logger.warning("web_cli_commands_timeout", project=key)
    except FileNotFoundError:
        logger.warning("web_cli_commands_not_found")
    except Exception as e:  # noqa: BLE001
        logger.warning("web_cli_commands_error", error=str(e))
    _cli_cmd_cache[key] = (now, [])
    return []


def cli_command_label(cmd: str) -> str:
    """Человеческое описание команды (переиспользует словарь бота)."""
    return _KNOWN_DESCRIPTIONS.get(cmd, _make_description(cmd))
