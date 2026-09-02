"""Message handlers for chat with Claude Code."""
from __future__ import annotations

import asyncio
import time
import uuid
from html import escape
from pathlib import Path

from aiogram import Bot, Router
from aiogram.types import Message

import structlog

from src.apikeys.policy import NeedsApiKeyError, resolve_session_auth
from src.bot import onboarding
from src.bot.single_project import bind_topic_to_single_project
from src.bot.keyboards import (
    create_mcp_candidate_keyboard,
    create_mcp_catalog_keyboard,
    create_model_keyboard,
)
from src.bot.permissions import is_bot_admin, is_bot_privileged
from src.claude import ClaudeBridge, ClaudeEventType, SessionManager, SessionStatus
from src.claude.models import (
    DEFAULT_MODEL,
    model_hint,
    model_label,
    normalize_model_id,
    resolve_model_input,
)
from src.claude.commands import refresh_commands_if_needed, resolve_command
from src.claude.mcp import detect_mcp_candidate, get_mcp_manager
from src.claude.native_commands import TUI_COMMANDS as _TUI_COMMANDS
from src.config import Settings
from src.connections.resolver import build_mcp_servers
from src.event_bus import AgentFinished, AgentStreamingUpdate, EventBus, UserMessageReceived
from src.utils import ResponseStreamer
from src.utils.context_hint import CONTEXT_WINDOW

logger = structlog.get_logger()

_MAX_LOG_LINES = 20  # Only last N log lines kept in memory (display uses last 8)

# TUI-only commands (/config /mcp /model /permissions) — единый источник в
# src.claude.native_commands.TUI_COMMANDS (импортирован как _TUI_COMMANDS).


def _format_error_for_user(error_type: str, raw_content: str) -> str:
    """Build a user-friendly error message from error_type metadata."""
    return onboarding.claude_error_message(error_type, raw_content)


def mcp_servers_for(store, user_id: int):
    """Per-user MCP servers for dispatch, or None when connections are disabled
    or the user has none. Never log the result — it holds decrypted secrets."""
    if store is None:
        return None
    servers = build_mcp_servers(user_id, store)
    return servers or None


router = Router(name="messages")


def setup_message_handlers(
    dp_router: Router,
    settings: Settings,
    session_manager: SessionManager,
    claude_bridge: ClaudeBridge,
    streamer: ResponseStreamer,
    bot: Bot,
    event_bus: EventBus | None = None,
    connections_store=None,
    api_key_store=None,
) -> None:
    """Register message handlers with the router."""
    router.settings = settings  # type: ignore
    router.session_manager = session_manager  # type: ignore
    router.claude_bridge = claude_bridge  # type: ignore
    router.streamer = streamer  # type: ignore
    router.bot = bot  # type: ignore
    router.event_bus = event_bus  # type: ignore
    router.connections_store = connections_store  # type: ignore
    # SP2 per-user Anthropic keys. None when CONNECTIONS_SECRET_KEY is unset,
    # which keeps the auth gate dormant (every session uses owner creds).
    router.api_key_store = api_key_store  # type: ignore
    router.mcp_manager = get_mcp_manager()  # type: ignore

    dp_router.include_router(router)


async def _typing_loop(bot: Bot, chat_id: int, topic_id: int | None) -> None:
    """Send typing action every 4 seconds until cancelled.
    
    Inner try/except keeps the loop alive through network errors.
    """
    try:
        while True:
            try:
                await bot.send_chat_action(
                    chat_id,
                    action="typing",
                    message_thread_id=topic_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("typing_action_failed", error=str(e))
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def _heartbeat_loop(
    streamer: ResponseStreamer,
    state,  # StreamingState — avoid circular import
    log_state: dict,
    start_time: float,
    verbose_level: int,
) -> None:
    """Update log every 2 seconds with elapsed time, even when Claude is silent.

    Delegates to _update_log_display so the display is consistent with
    event-driven updates — tool headers are never overwritten by raw logs.
    """
    try:
        while True:
            await asyncio.sleep(2)
            await _update_log_display(streamer, state, log_state, start_time, verbose_level)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug("heartbeat_failed", error=str(e))


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds into a human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}с"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}м {secs}с"


_TOOL_EMOJI_PREFIXES = ("\U0001f527 ", "\U0001f50d ", "\U0001f4c4 ")


def _extract_tool_name(formatted: str) -> str:
    """Extract clean tool name from formatted content (e.g. '🔧 Bash' → 'Bash')."""
    first_line = formatted.split("\n")[0]
    for prefix in _TOOL_EMOJI_PREFIXES:
        if first_line.startswith(prefix):
            return first_line[len(prefix):].strip()
    return first_line.strip()


def _format_context_header(cumulative: dict | int | None) -> str:
    """Format context usage like '123K / 200K (61.7%)'.

    Accepts either a pre-computed int (real context tokens) or a dict with
    cumulative usage keys ('total_input_tokens', 'total_cache_read_tokens',
    'total_cache_creation_tokens'). Returns empty string if no data.
    """
    if not cumulative:
        return ""

    if isinstance(cumulative, int):
        context = cumulative
    else:
        total_input = cumulative.get("total_input_tokens", 0)
        total_cache_read = cumulative.get("total_cache_read_tokens", 0)
        total_cache_write = cumulative.get("total_cache_creation_tokens", 0)
        context = total_input + total_cache_read + total_cache_write

    if context <= 0:
        return ""

    limit = CONTEXT_WINDOW
    pct = (context / limit) * 100

    def fmt(n: int) -> str:
        if n >= 1_000_000:
            v = n / 1_000_000
            return f"{v:.0f}M" if v == int(v) else f"{v:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.0f}K"
        return str(n)

    return f"{fmt(context)} / {fmt(limit)} ({pct:.1f}%)"


async def _stream_subagent_status(
    streamer: ResponseStreamer,
    state,
    text: str,
    verbose_level: int,
) -> None:
    """Stream subagent status into the main response flow."""
    if verbose_level > 0:
        await streamer.stream_response(state, text)
    else:
        state.response_buffer += text


async def _update_tool_status(
    log_state: dict,
    finished_tool: str | None,
) -> None:
    """Determine tool success/failure and record status."""
    if not finished_tool:
        return

    tool_outputs = log_state.get("tool_outputs", {}).get(finished_tool, [])
    all_lines = log_state.get("lines", [])

    # Only record status if tool had captured outputs; otherwise status
    # would be based solely on global log lines and likely inaccurate.
    if not tool_outputs:
        return

    # Scan recent lines for error indicators using word-boundary patterns
    # to avoid false positives like "No errors found" or "error handling complete".
    error_patterns = [
        r"\berror\b", r"\bfailed\b", r"exit code [1-9]",
        r"\bexception\b", r"\btraceback\b",
    ]
    import re
    has_error = any(
        any(re.search(pattern, line.lower()) for pattern in error_patterns)
        for line in (tool_outputs + all_lines[-10:])
    )

    log_state["tool_status"][finished_tool] = {
        "success": not has_error,
        "error_msg": "Failed" if has_error else None,
    }


async def _update_log_display(
    streamer: ResponseStreamer,
    state,
    log_state: dict,
    start_time: float,
    verbose_level: int,
) -> None:
    """Update log display based on verbosity level."""
    elapsed = _format_elapsed(time.time() - start_time)
    count = log_state["count"]
    context_str = _format_context_header(log_state.get("real_context", 0))
    if context_str:
        header = f"\u23f3 {elapsed} \u00b7 {count} tools \u00b7 {context_str}"
    else:
        header = f"\u23f3 {elapsed} \u00b7 {count} tools"

    if verbose_level == 2:
        # Level 2: show full multi-line tool headers + captured outputs
        display_lines = []
        for hdr in state.tool_header_lines[-6:]:  # fewer tools due to multi-line
            display_lines.append(hdr)
            # Append captured outputs for this tool
            tool_name = _extract_tool_name(hdr)
            outputs = log_state.get("tool_outputs", {}).get(tool_name, [])
            if outputs:
                display_lines.extend("   " + line for line in outputs)
        log_display = header + "\n\n" + "\n".join(display_lines) if display_lines else header
    elif verbose_level == 3:
        # Level 3: single-line headers with status indicators
        display_lines = []
        for hdr in state.tool_header_lines[-8:]:
            tool_name = _extract_tool_name(hdr)
            status = log_state.get("tool_status", {}).get(tool_name)
            if status:
                icon = "✅" if status.get("success") else "❌"
                display_lines.append(f"{hdr} {icon}")
            else:
                display_lines.append(hdr)
        log_display = header + "\n\n" + "\n".join(display_lines) if display_lines else header
    else:
        # Level 1: single-line headers
        log_display = header + "\n\n" + "\n".join(state.tool_header_lines[-8:]) if state.tool_header_lines else header

    state.last_log_update = 0
    await streamer.update_log(state, log_display)


async def _handle_event(
    event_type: ClaudeEventType,
    content: str,
    metadata: dict | None,
    *,
    streamer: ResponseStreamer,
    state,
    log_state: dict,
    start_time: float,
    verbose_level: int,
    enable_subagent_tracking: bool,
    session_manager: SessionManager,
    topic_id: int,
    settings: Settings,
) -> dict | None:
    """Process a single Claude event. Shared by event-bus and direct-stream paths.

    Returns a dict with side-effect signals:
        - ``{"response_text": str}`` for TEXT events
        - ``{"session_id": str}`` for INIT events
        - ``{"usage": dict, "session_id": str | None}`` for COMPLETE events
        - ``{"error": True, "error_msg": str}`` for ERROR events
        - ``None`` otherwise
    """
    if event_type == ClaudeEventType.TOOL_INPUT:
        return None

    if event_type == ClaudeEventType.LOG:
        stripped = content.strip()
        if stripped and len(stripped) > 2:
            log_state["lines"].append(stripped)
            log_state["lines"] = log_state["lines"][-_MAX_LOG_LINES:]

            # Level 2/3: capture output lines for current tool
            if verbose_level >= 2 and log_state.get("current_tool"):
                # Filter out Claude Code system log lines (timestamps, "Claude Code:" prefix)
                if not stripped.startswith("20") and "Claude Code:" not in stripped:
                    tool_name = log_state["current_tool"]
                    if tool_name in log_state.get("tool_outputs", {}):
                        log_state["tool_outputs"][tool_name].append(stripped)
                        # Keep last 3 lines per tool to avoid flooding
                        if len(log_state["tool_outputs"][tool_name]) > 3:
                            log_state["tool_outputs"][tool_name] = log_state["tool_outputs"][tool_name][-3:]
        return None

    if event_type == ClaudeEventType.TOOL_USE:
        # Finalize previous tool (level 3 status)
        prev_tool = log_state.get("current_tool")
        if prev_tool and verbose_level >= 3:
            await _update_tool_status(log_state, prev_tool)

        log_state["count"] += 1
        tool_name = content.strip()
        clean_name = _extract_tool_name(content)

        if verbose_level >= 1:
            first_line = content.split("\n")[0]
            if verbose_level == 1:
                # Level 1: tool name only (first line)
                state.tool_header_lines.append(first_line)
            elif verbose_level == 2:
                # Level 2: full formatted output
                state.tool_header_lines.append(content)
                # Track current tool for LOG capture
                log_state["current_tool"] = clean_name
                log_state["tool_outputs"][clean_name] = []
            elif verbose_level == 3:
                # Level 3: full output, track for status
                state.tool_header_lines.append(content)
                log_state["current_tool"] = clean_name
                log_state["tool_outputs"][clean_name] = []

        if settings.display.show_logs and verbose_level > 0:
            await _update_log_display(streamer, state, log_state, start_time, verbose_level)
        return None

    if event_type == ClaudeEventType.SUBAGENT_START:
        await _stream_subagent_status(
            streamer, state, "🕵️‍♂️ Autonomous researcher launched...", verbose_level,
        )
        return None

    if event_type == ClaudeEventType.SUBAGENT_LOG:
        if enable_subagent_tracking:
            await _stream_subagent_status(
                streamer, state, content, verbose_level,
            )
        return None

    if event_type == ClaudeEventType.SUBAGENT_FINISH:
        await _stream_subagent_status(
            streamer, state, "✅ Researcher finished its job.", verbose_level,
        )
        return None

    if event_type == ClaudeEventType.TEXT:
        # Stream character-by-character for smooth Telegram display
        if verbose_level > 0:
            for char in content:
                await streamer.stream_response(state, char)
        else:
            state.response_buffer += content
        return {"response_text": content}

    if event_type == ClaudeEventType.INIT:
        session_id = (metadata or {}).get("session_id")
        if session_id:
            await session_manager.async_update_session_id(topic_id, session_id)
        return None

    if event_type == ClaudeEventType.USAGE:
        # Store usage data from USAGE event for later use in COMPLETE
        if metadata:
            usage = metadata.get("usage", {})
            state._last_usage = usage  # type: ignore
            logger.debug("usage_event_received", usage=usage)

            # SDK uses different key names than our internal format
            cache_read = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))
            cache_create = usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens", 0))
            inp = usage.get("input_tokens", 0)

            # Real context size = new input + cached context
            real_context = inp + cache_read + cache_create
            log_state["real_context"] = real_context

            # Update cumulative so heartbeat shows context immediately
            cumulative = log_state.get("cumulative_usage")
            if cumulative and usage:
                key_map = {
                    "input_tokens": "total_input_tokens",
                    "output_tokens": "total_output_tokens",
                    "cache_read_input_tokens": "total_cache_read_tokens",
                    "cache_creation_input_tokens": "total_cache_creation_tokens",
                    "cache_read_tokens": "total_cache_read_tokens",
                    "cache_creation_tokens": "total_cache_creation_tokens",
                }
                for src_key, dst_key in key_map.items():
                    if src_key in usage:
                        cumulative[dst_key] = cumulative.get(dst_key, 0) + usage[src_key]

            # Trigger immediate log update to show context in header
            if settings.display.show_logs and verbose_level > 0:
                await _update_log_display(streamer, state, log_state, start_time, verbose_level)
        return None

    if event_type == ClaudeEventType.COMPLETE:
        # Finalize last tool (level 3 status)
        prev_tool = log_state.get("current_tool")
        if prev_tool and verbose_level >= 3:
            await _update_tool_status(log_state, prev_tool)

        result: dict = {}
        if metadata:
            usage = metadata.get("usage")
            # Fallback to last received usage if COMPLETE has no usage
            if not usage and hasattr(state, "_last_usage") and state._last_usage:  # type: ignore
                usage = state._last_usage  # type: ignore
                logger.debug("complete_event_using_fallback_usage", usage=usage)
            logger.debug("complete_event_metadata", has_usage=usage is not None, usage=usage)
            if usage:
                result["usage"] = usage
            sid = metadata.get("session_id")
            if sid:
                result["session_id"] = sid
        else:
            logger.debug("complete_event_no_metadata")
        return result

    if event_type == ClaudeEventType.ERROR:
        await session_manager.async_set_status(topic_id, SessionStatus.ERROR)
        error_type = (metadata or {}).get("error_type", "unknown")
        error_msg = _format_error_for_user(error_type, content)
        logger.error(
            "claude_error_event",
            topic_id=topic_id,
            error_type=error_type,
            error_content=content,
            metadata=metadata,
        )
        if error_type == "context_overflow" or "контекст" in error_msg.lower():
            await session_manager.async_clear_session_id(topic_id)
        return {"error": True, "error_msg": error_msg}

    return None


# Mapping from AgentStreamingUpdate.kind strings to ClaudeEventType
_KIND_TO_EVENT_TYPE: dict[str, ClaudeEventType] = {
    "log": ClaudeEventType.LOG,
    "text": ClaudeEventType.TEXT,
    "tool_use": ClaudeEventType.TOOL_USE,
    "tool_input": ClaudeEventType.TOOL_INPUT,
    "subagent_start": ClaudeEventType.SUBAGENT_START,
    "subagent_log": ClaudeEventType.SUBAGENT_LOG,
    "subagent_finish": ClaudeEventType.SUBAGENT_FINISH,
    "init": ClaudeEventType.INIT,
    "usage": ClaudeEventType.USAGE,
}


class _MessageBusRun:
    def __init__(
        self,
        *,
        request_id: str,
        message: Message,
        topic_id: int,
        settings: Settings,
        session_manager: SessionManager,
        claude_bridge: ClaudeBridge,
        streamer: ResponseStreamer,
        state,
        start_time: float,
        log_state: dict,
        verbose_level: int,
        enable_subagent_tracking: bool,
    ) -> None:
        self.request_id = request_id
        self.message = message
        self.topic_id = topic_id
        self.settings = settings
        self.session_manager = session_manager
        self.claude_bridge = claude_bridge
        self.streamer = streamer
        self.state = state
        self.start_time = start_time
        self.log_state = log_state
        self.verbose_level = verbose_level
        self.enable_subagent_tracking = enable_subagent_tracking
        self.response_text = ""
        self.usage_data = None
        self.error_occurred = False

    def subscribe(self, event_bus: EventBus) -> callable:
        unsubscribers = [
            event_bus.subscribe(AgentStreamingUpdate, self._handle_update),
            event_bus.subscribe(AgentFinished, self._handle_finished),
        ]

        def unsubscribe() -> None:
            for unsubscribe_one in unsubscribers:
                unsubscribe_one()

        return unsubscribe

    async def _handle_update(self, event: AgentStreamingUpdate) -> None:
        if event.request_id != self.request_id:
            return

        event_type = _KIND_TO_EVENT_TYPE.get(event.kind)
        if event_type is None:
            return

        result = await _handle_event(
            event_type,
            event.content,
            getattr(event, "metadata", None),
            streamer=self.streamer,
            state=self.state,
            log_state=self.log_state,
            start_time=self.start_time,
            verbose_level=self.verbose_level,
            enable_subagent_tracking=self.enable_subagent_tracking,
            session_manager=self.session_manager,
            topic_id=self.topic_id,
            settings=self.settings,
        )
        if result and "response_text" in result:
            self.response_text += result["response_text"]

    async def _handle_finished(self, event: AgentFinished) -> None:
        if event.request_id != self.request_id:
            return

        self.usage_data = event.usage
        # Accumulate any response_text from AgentFinished (fallback for missed TEXT events)
        if event.response_text:
            self.response_text += event.response_text
        if event.error:
            self.error_occurred = True
            await self.session_manager.async_set_status(self.topic_id, SessionStatus.ERROR)
            await self._handle_error(event.error)
            return

        if event.session_id:
            await self.session_manager.async_update_session_id(self.topic_id, event.session_id)

    async def _handle_error(self, error_text: str) -> None:
        context_keywords = (
            "chunk is longer than limit",
            "prompt is too long",
            "context_length_exceeded",
            "context window",
            "too many tokens",
            "exceeds maximum",
        )
        is_context_overflow = any(keyword in error_text.lower() for keyword in context_keywords)
        if is_context_overflow:
            await self.session_manager.async_clear_session_id(self.topic_id)
            self.claude_bridge.clear_session_cache(self.topic_id)
            await self.message.answer(
                "⚠️ <b>Контекст сессии переполнен</b>\n\n"
                "История диалога слишком большая для Claude.\n\n"
                "Попробуйте:\n"
                "• <code>/compact</code> — сжать историю\n"
                "• <code>/clear</code> — очистить историю\n"
                "• <code>/close</code> — закрыть сессию и начать новую",
                parse_mode="HTML",
            )
            return

        await self.message.answer(f"❌ Ошибка: {error_text}")


# Module-level cache for forum topic icon sticker IDs
_icon_sticker_ids: list[str] = []

# Telegram's 6 built-in topic icon colors (RGB)
_TOPIC_COLORS = [7322096, 16766590, 13338331, 9367192, 16752050, 16478047]


async def _get_icon_emoji_id(bot: Bot, seed: str) -> str | None:
    """Return a deterministic forum topic icon emoji ID based on *seed*.

    Sticker IDs are fetched once from Telegram and cached for the process
    lifetime.  Returns None on any failure so callers can fall back gracefully.
    """
    global _icon_sticker_ids
    if not _icon_sticker_ids:
        try:
            stickers = await bot.get_forum_topic_icon_stickers()
            _icon_sticker_ids = [
                s.custom_emoji_id
                for s in stickers
                if getattr(s, "custom_emoji_id", None)
            ]
        except Exception as e:
            logger.debug("icon_stickers_fetch_failed", error=str(e))
            return None
    if not _icon_sticker_ids:
        return None
    # Deterministic simple hash for consistent emoji assignment
    h = 0
    for char in seed:
        h = (31 * h + ord(char)) & 0xFFFFFFFF
    return _icon_sticker_ids[h % len(_icon_sticker_ids)]


async def _rename_topic(
    bot: Bot,
    chat_id: int,
    topic_id: int,
    project_name: str,
    first_message: str,
) -> None:
    """Rename forum topic to '[emoji] Project · Message preview' on first chat message.

    Format: "🚀 Youtube · Анализ топ-видео за март"
    Max Telegram topic name length: 128 chars.
    """
    try:
        # Shorten project name: take up to 2 words, max 20 chars
        words = project_name.split()
        short_project = " ".join(words[:2])[:20]

        # Shorten message: strip newlines, take first line, trim
        short_msg = first_message.replace("\n", " ").strip()
        # Remove slash-commands prefix for readability
        if short_msg.startswith("/"):
            short_msg = short_msg.lstrip("/").strip()

        # Build combined name
        separator = " · "
        max_msg_len = 128 - len(short_project) - len(separator) - 1
        if len(short_msg) > max_msg_len:
            short_msg = short_msg[: max_msg_len - 1] + "…"

        topic_name = f"{short_project}{separator}{short_msg}" if short_msg else short_project
        topic_name = topic_name[:128]

        # Pick a deterministic emoji icon for this project
        emoji_id = await _get_icon_emoji_id(bot, project_name)

        kwargs: dict = {"chat_id": chat_id, "message_thread_id": topic_id, "name": topic_name}
        if emoji_id:
            kwargs["icon_custom_emoji_id"] = emoji_id

        await bot.edit_forum_topic(**kwargs)
        logger.info("topic_renamed", topic_id=topic_id, name=topic_name, emoji=bool(emoji_id))
    except Exception as e:
        # Silently ignore — no permission, General topic, etc.
        logger.warning("topic_rename_failed", error=str(e))


async def _handle_native_command(cmd: str, full_text: str, message: Message) -> bool:
    """Handle TUI-only commands that Claude CLI doesn't support via -p.
    Returns True if handled."""
    # Общий атомарный I/O для ~/.claude/settings.json — тот же модуль, что
    # использует web /api/model. Раньше здесь был неатомарный write_text,
    # который при крахе мог оставить битый JSON (CR3-19).
    from src.claude.claude_settings import read_claude_settings as _read_settings
    from src.claude.claude_settings import write_claude_settings as _write_settings

    if cmd == "/config":
        data = _read_settings()
        if not data:
            await message.answer("⚙️ Конфиг пуст (~/.claude/settings.json).")
            return True
        # Безопасность: НЕ дампим сырой settings.json (в нём env-прокси-креды,
        # apiKeyHelper, MCP-токены). Переиспользуем общий редактированный
        # рендерер (тот же, что и веб) — единый источник правды.
        from src.claude.native_commands import render_config_report
        from src.utils.formatter import format_for_telegram

        await message.answer(
            format_for_telegram(render_config_report()), parse_mode="HTML"
        )
        return True

    if cmd == "/model":
        session_manager: SessionManager = router.session_manager  # type: ignore
        user_id = message.from_user.id if message.from_user else None
        parts = full_text.strip().split(maxsplit=1)

        if len(parts) > 1:
            # Set model via text alias: /model opus — admin only.
            if not await is_bot_admin(session_manager, user_id):
                await message.answer(
                    "🔒 Сменить модель может только администратор."
                )
                return True

            resolved = resolve_model_input(parts[1])
            if resolved is None:
                await message.answer(
                    f"❌ Неизвестная модель «{escape(parts[1].strip())}».\n\n"
                    "Доступно: <code>/model opus</code>, "
                    "<code>/model sonnet</code>, <code>/model haiku</code>",
                    parse_mode="HTML",
                )
                return True

            data = await asyncio.to_thread(_read_settings)
            old_model = normalize_model_id(str(data.get("model") or ""))
            data["model"] = resolved
            await asyncio.to_thread(_write_settings, data)

            topic_id = message.message_thread_id
            if topic_id and old_model != resolved:
                claude_bridge: ClaudeBridge = router.claude_bridge  # type: ignore
                await session_manager.async_clear_session_id(topic_id)
                claude_bridge.clear_session_cache(topic_id)

            await message.answer(
                f"✅ Модель изменена на <b>{model_label(resolved)}</b>\n\n"
                "Следующее сообщение начнёт новую сессию с этой моделью.",
                reply_markup=create_model_keyboard(resolved),
                parse_mode="HTML",
            )
            return True

        # No argument — show current model + picker keyboard (all users).
        data = await asyncio.to_thread(_read_settings)
        current = normalize_model_id(str(data.get("model") or DEFAULT_MODEL))
        hint = model_hint(current)
        is_admin = await is_bot_admin(session_manager, user_id)
        body = (
            f"🤖 <b>Модель Claude</b>\n\n"
            f"Текущая: <b>{model_label(current)}</b>\n"
            f"{hint}\n\n"
            f"Выберите модель кнопкой ниже:"
        )
        if not is_admin:
            body += "\n\nℹ️ Сменить модель может только администратор."
        await message.answer(
            body,
            reply_markup=create_model_keyboard(current),
            parse_mode="HTML",
        )
        return True

    if cmd == "/mcp":
        manager = getattr(router, "mcp_manager", get_mcp_manager())
        await message.answer(
            manager.describe_catalog(),
            reply_markup=create_mcp_catalog_keyboard(manager.installed_server_names()),
            parse_mode="HTML",
        )
        return True

    if cmd == "/permissions":
        # Безопасность: НЕ раскрываем allow/deny firewall-паттерны — только
        # режим и счётчики правил (общий редактированный рендерер, как в вебе).
        from src.claude.native_commands import render_permissions_report
        from src.utils.formatter import format_for_telegram

        await message.answer(
            format_for_telegram(render_permissions_report()), parse_mode="HTML"
        )
        return True

    return False


async def process_incoming_text(message: Message, msg_text: str) -> None:
    """Process user input through the standard typed-message flow."""
    settings: Settings = router.settings  # type: ignore
    session_manager: SessionManager = router.session_manager  # type: ignore
    claude_bridge: ClaudeBridge = router.claude_bridge  # type: ignore
    streamer: ResponseStreamer = router.streamer  # type: ignore
    bot: Bot = router.bot  # type: ignore
    event_bus: EventBus | None = getattr(router, "event_bus", None)  # type: ignore

    topic_id = message.message_thread_id

    # If user writes in General chat — auto-create forum topic
    if not topic_id:
        try:
            topic_name = "Новая сессия Vels Claude Light"

            # Deterministic color for this chat (cycles through 6 options)
            color = _TOPIC_COLORS[message.chat.id % len(_TOPIC_COLORS)]

            # Fetch and pick emoji icon
            emoji_id = await _get_icon_emoji_id(bot, topic_name)

            create_kwargs: dict = {
                "chat_id": message.chat.id,
                "name": topic_name,
                "icon_color": color,
            }
            if emoji_id:
                create_kwargs["icon_custom_emoji_id"] = emoji_id

            new_topic = await bot.create_forum_topic(**create_kwargs)
            topic_id = new_topic.message_thread_id

            logger.info(
                "auto_topic_created",
                topic_id=topic_id,
                topic_name=topic_name,
            )

            # Light: проект один, выбирать не из чего — привязываем сразу.
            # В вебе чат так же создаётся в единственном проекте, без вопроса.
            bound = await bind_topic_to_single_project(
                settings=settings,
                session_manager=session_manager,
                topic_id=topic_id,
                chat_id=message.chat.id,
            )
            if bound is not None:
                await bot.send_message(
                    chat_id=message.chat.id,
                    message_thread_id=topic_id,
                    text=onboarding.new_session_prompt_message(auto_created=True),
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=message.chat.id,
                    message_thread_id=topic_id,
                    text=onboarding.no_projects_message(settings.get_projects_directory()),
                    parse_mode="HTML",
                )
            return
        except Exception as e:
            logger.error("auto_create_topic_failed", error=str(e), chat_id=message.chat.id)
            await message.answer(onboarding.auto_topic_failure_message())
            return

    # Check if session exists
    candidate = detect_mcp_candidate(msg_text)
    if candidate:
        manager = getattr(router, "mcp_manager", get_mcp_manager())
        token = manager.register_candidate(candidate)
        await message.answer(
            "🔌 <b>Добавить MCP?</b>\n\n"
            f"<b>Инструмент:</b> <code>{escape(candidate.server_name)}</code>\n"
            f"<b>Источник:</b> <code>{escape(candidate.install_spec)}</code>",
            reply_markup=create_mcp_candidate_keyboard(token),
            parse_mode="HTML",
        )
        return

    session = await session_manager.async_get_session(topic_id)

    if not session:
        projects = settings.get_light_project_paths()
        logger.info(
            "no_session — prompting project selection",
            topic_id=topic_id,
            projects_available=len(projects),
        )

        if not projects:
            await message.answer(
                onboarding.no_projects_message(settings.get_projects_directory()),
                parse_mode="HTML",
            )
            return

        # Проект один — привязываем и продолжаем обработку этого же сообщения,
        # чтобы человек не отправлял его повторно после нажатия кнопки.
        bound = await bind_topic_to_single_project(
            settings=settings,
            session_manager=session_manager,
            topic_id=topic_id,
            chat_id=message.chat.id,
        )
        if bound is None:
            await message.answer(
                onboarding.no_projects_message(settings.get_projects_directory()),
                parse_mode="HTML",
            )
            return
        session = await session_manager.async_get_session(topic_id)
        if session is None:
            return

    cmd_word = msg_text.strip().split()[0].lower() if msg_text.strip() else ""
    if cmd_word in _TUI_COMMANDS:
        logger.info("⚙️ native_command", command=cmd_word, topic_id=topic_id)
        handled = await _handle_native_command(cmd_word, msg_text, message)
        if handled:
            return

    project_path = Path(session.project_path).expanduser()
    if not project_path.exists():
        await session_manager.async_set_status(topic_id, SessionStatus.ERROR)
        await message.answer(
            onboarding.project_missing_message(str(project_path)),
            parse_mode="HTML",
        )
        return

    # SP2 auth gate: decide which Anthropic credentials this session runs
    # against BEFORE dispatching to Claude. Mirrors the event-bus relay logic
    # (Task 7) on the bot side. Unprivileged callers may be required to bring
    # their own key (Path B, "everyone pays with their own key"); privileged
    # callers (bot admins) fall back to owner credentials. The store is None
    # when CONNECTIONS_SECRET_KEY is unset — then the gate stays dormant and
    # every session uses owner creds (graceful degradation).
    auth_user_id = message.from_user.id if message.from_user else 0
    api_key_store = getattr(router, "api_key_store", None)
    # Privileged = whitelist OR is_admin (parity with web). The owner rides
    # their own subscription: Telegram login yields is_admin=0, so gating on
    # is_bot_admin alone would lock the owner out and force them to /apikey.
    allowed_ids = getattr(settings, "get_allowed_user_ids", list)()
    is_privileged = await is_bot_privileged(session_manager, auth_user_id, allowed_ids)
    user_key = api_key_store.get_key(auth_user_id) if api_key_store is not None else None
    require_user_key = (
        bool(getattr(settings, "require_user_key", True))
        if api_key_store is not None
        else False
    )
    try:
        auth_decision = resolve_session_auth(
            is_privileged=is_privileged,
            user_key=user_key,
            require_user_key=require_user_key,
        )
    except NeedsApiKeyError:
        await message.answer(
            "Добавьте свой Anthropic API-ключ через команду:\n/apikey"
        )
        return
    api_key_to_inject = auth_decision.api_key

    # Refresh slash commands in Telegram menu if stale (runs in background)
    await refresh_commands_if_needed(bot)

    # Convert Telegram command names back to CLI format
    # e.g. "/superpowers_brainstorm args" → "/superpowers:brainstorm args"
    if msg_text.startswith("/"):
        parts = msg_text.split(None, 1)
        resolved = resolve_command(parts[0])
        if resolved != parts[0]:
            msg_text = resolved + (" " + parts[1] if len(parts) > 1 else "")

    # Send message to Claude Code
    logger.info(
        "sending_to_claude",
        topic_id=topic_id,
        project=session.project_name,
        message_length=len(msg_text),
    )

    # On first message: rename topic to "Project · Query preview"
    if not session.is_renamed and topic_id:
        await session_manager.async_mark_renamed(topic_id)
        asyncio.create_task(
            _rename_topic(bot, message.chat.id, topic_id, session.project_name, msg_text)
        )

    # Get verbose level for this session
    verbose_level = session.verbose_level if hasattr(session, "verbose_level") else 1

    # Update session status to working
    await session_manager.async_set_status(topic_id, SessionStatus.WORKING)

    # Create streaming state
    state = await streamer.create_log_message(
        chat_id=message.chat.id,
        topic_id=topic_id,
    )

    start_time = time.time()

    # Get cumulative usage from previous requests in this session
    cumulative_usage = await session_manager.async_get_usage(topic_id) if topic_id else None

    # Shared mutable state for heartbeat
    log_state: dict = {
        "lines": [],
        "count": 0,
        "current_tool": None,       # (name) for level 2 LOG capture
        "tool_outputs": {},         # tool_name -> [output_lines] for level 2
        "tool_status": {},          # tool_name -> {success: bool, error_msg: str|None} for level 3
        "cumulative_usage": cumulative_usage,
        "current_input_tokens": 0,  # real context size from Claude's response
    }
    enable_subagent_tracking = getattr(session, "enable_subagent_tracking", False)

    # Start typing indicator (survives network errors)
    typing_task = asyncio.create_task(
        _typing_loop(bot, message.chat.id, topic_id)
    )

    # Start heartbeat — ticks log every 2s even when Claude is silent
    # Skip heartbeat at verbose_level 0 (quiet mode — only typing indicator)
    heartbeat_task: asyncio.Task | None = None
    if verbose_level > 0:
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(streamer, state, log_state, start_time, verbose_level)
        )

    # Process with Claude Code
    response_text = ""
    usage_data = None
    error_occurred = False

    try:
        if event_bus is not None:
            request_id = uuid.uuid4().hex
            bus_run = _MessageBusRun(
                request_id=request_id,
                message=message,
                topic_id=topic_id,
                settings=settings,
                session_manager=session_manager,
                claude_bridge=claude_bridge,
                streamer=streamer,
                state=state,
                start_time=start_time,
                log_state=log_state,
                verbose_level=verbose_level,
                enable_subagent_tracking=enable_subagent_tracking,
            )
            unsubscribe = bus_run.subscribe(event_bus)
            try:
                await event_bus.publish(
                    UserMessageReceived(
                        request_id=request_id,
                        chat_id=message.chat.id,
                        user_id=message.from_user.id if message.from_user else 0,
                        topic_id=topic_id,
                        session_uuid=session.session_uuid,
                        project_path=session.project_path,
                        project_name=session.project_name,
                        text=msg_text,
                        session_id=session.session_id,
                        source="telegram",
                        privileged=is_privileged,
                    )
                )
            finally:
                unsubscribe()
            response_text = bus_run.response_text
            usage_data = bus_run.usage_data
            error_occurred = bus_run.error_occurred
        else:
            user_id = message.from_user.id if message.from_user else 0
            mcp_servers = mcp_servers_for(getattr(router, "connections_store", None), user_id)
            async for event in claude_bridge.send_message(
                message=msg_text,
                topic_id=topic_id,
                project_path=session.project_path,
                session_id=session.session_id,
                mcp_servers=mcp_servers,
                anthropic_api_key=api_key_to_inject,
            ):
                result = await _handle_event(
                    event.type,
                    event.content,
                    event.metadata,
                    streamer=streamer,
                    state=state,
                    log_state=log_state,
                    start_time=start_time,
                    verbose_level=verbose_level,
                    enable_subagent_tracking=enable_subagent_tracking,
                    session_manager=session_manager,
                    topic_id=topic_id,
                    settings=settings,
                )
                if result is None:
                    continue
                if "response_text" in result:
                    response_text += result["response_text"]
                if "usage" in result:
                    usage_data = result.get("usage")
                    sid = result.get("session_id")
                    if sid and not error_occurred:
                        await session_manager.async_update_session_id(topic_id, sid)
                if "error" in result:
                    error_occurred = True
                    if "контекст" in result.get("error_msg", "").lower():
                        claude_bridge.clear_session_cache(topic_id)
                    await message.answer(result["error_msg"], parse_mode="HTML")

    except Exception as e:
        error_occurred = True
        await session_manager.async_set_status(topic_id, SessionStatus.ERROR)
        logger.error(
            "❌ claude_error",
            error=str(e),
            topic_id=topic_id,
            project=session.project_name,
            elapsed=f"{time.time() - start_time:.1f}s",
        )
        await message.answer(f"\u274c Ошибка при работе с Claude Code: {e}")
    finally:
        for task in (typing_task, heartbeat_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # If Claude returned no text response but had tool log output,
    # show the log content as the response
    if not response_text.strip() and log_state["lines"] and not error_occurred:
        state.response_buffer = "\n".join(log_state["lines"])
        logger.info(
            "using_log_as_response",
            topic_id=topic_id,
            log_lines=len(log_state["lines"]),
        )

    # Accumulate token usage in session
    cumulative_usage = None
    if usage_data and isinstance(usage_data, dict) and not error_occurred:
        inp = usage_data.get("input_tokens", 0)
        out = usage_data.get("output_tokens", 0)
        cache_read = usage_data.get("cache_read_tokens", 0)
        cache_create = usage_data.get("cache_creation_tokens", 0)
        cost_usd = usage_data.get("cost_usd", 0.0)
        logger.debug(
            "usage_data_received",
            topic_id=topic_id,
            input=inp,
            output=out,
            cache_read=cache_read,
            cache_create=cache_create,
            cost=cost_usd,
        )
        if inp > 0 or out > 0 or cache_read > 0 or cache_create > 0:
            await session_manager.async_add_usage(
                topic_id, inp, out,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_create,
                cost_usd=cost_usd,
            )
            logger.info("usage_added", topic_id=topic_id, total=inp+out+cache_read+cache_create)
        cumulative_usage = await session_manager.async_get_usage(topic_id)
        logger.debug("cumulative_usage", topic_id=topic_id, cumulative=cumulative_usage)
    else:
        logger.debug("no_usage_data", topic_id=topic_id, usage_data=usage_data)

    # Update status to done if no error occurred
    if not error_occurred:
        await session_manager.async_set_status(topic_id, SessionStatus.DONE)

    # Always finalize (cleans up draft task, deletes log message)
    finalize_kwargs = {
        "show_token_usage": settings.display.show_token_usage,
        "show_context_usage": settings.display.show_context_usage,
        "usage": usage_data,
        "cumulative": cumulative_usage,
        "send_empty_completion": not error_occurred,
        "keep_log": settings.display.keep_log_after_response,
    }
    try:
        await streamer.finalize(
            state,
            **finalize_kwargs,
        )
    except TypeError as exc:
        if "send_empty_completion" not in str(exc):
            raise
        finalize_kwargs.pop("send_empty_completion")
        await streamer.finalize(
            state,
            **finalize_kwargs,
        )

    logger.info(
        "✅ response_complete",
        topic_id=topic_id,
        project=session.project_name,
        response_length=len(response_text),
        tools_used=log_state["count"],
        elapsed=f"{time.time() - start_time:.1f}s",
        error=error_occurred,
    )


@router.message()
async def handle_message(message: Message) -> None:
    """Handle incoming messages - send to Claude Code."""
    if not message.text:
        logger.debug("message_ignored", reason="no_text", chat_id=message.chat.id)
        return

    logger.info(
        "📩 incoming_message",
        from_user=message.from_user.username if message.from_user else "unknown",
        user_id=message.from_user.id if message.from_user else 0,
        chat_id=message.chat.id,
        topic_id=message.message_thread_id,
        text_preview=message.text[:80].replace("\n", " "),
    )

    await process_incoming_text(message, message.text)
