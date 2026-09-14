"""Bridge to Claude SDK/CLI with optional tmux-backed transport."""
from __future__ import annotations

import asyncio
import errno
import functools
import json
import os
import re
import shlex
import subprocess
import time
import traceback
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import structlog
from src.claude.errors import (
    ClaudeAuthError,
    ClaudeBridgeError,
    ClaudeContextOverflowError,
    ClaudeRateLimitError,
    ClaudeStallError,
    ClaudeTimeoutError,
)
from src.claude.bash_outputs import extract_bash_outputs
from src.claude.pty import TerminalOutputBuffer

logger = structlog.get_logger()

_STREAM_DONE = object()

# SDK exceptions — imported lazily for diagnostics
_SDK_EXCEPTIONS_AVAILABLE = False
_ProcessError: type[Exception] | None = None
_CLIConnectionError: type[Exception] | None = None
_ClaudeSDKError: type[Exception] | None = None


def _load_sdk_exceptions() -> None:
    """Import SDK exception types for better error classification."""
    global _SDK_EXCEPTIONS_AVAILABLE, _ProcessError, _CLIConnectionError, _ClaudeSDKError
    if _SDK_EXCEPTIONS_AVAILABLE:
        return
    try:
        from claude_agent_sdk import CLIConnectionError as _CLIConn
        from claude_agent_sdk import ClaudeSDKError
        from claude_agent_sdk import ProcessError as _ProcErr
        _ProcessError = _ProcErr
        _CLIConnectionError = _CLIConn
        _ClaudeSDKError = ClaudeSDKError
        _SDK_EXCEPTIONS_AVAILABLE = True
    except ImportError:
        pass


def _classify_error(exc: Exception) -> tuple[str, str]:
    """Classify an exception into an error_type and user-friendly message.

    Returns (error_type, message) where error_type is one of:
    "rate_limit", "context_overflow", "auth", "timeout", "stall", "unknown"
    """
    if isinstance(exc, ClaudeRateLimitError):
        return "rate_limit", str(exc)
    if isinstance(exc, ClaudeContextOverflowError):
        return "context_overflow", str(exc)
    if isinstance(exc, ClaudeAuthError):
        return "auth", str(exc)
    if isinstance(exc, ClaudeTimeoutError):
        return "timeout", str(exc)
    if isinstance(exc, ClaudeStallError):
        return "stall", str(exc)
    if isinstance(exc, ClaudeBridgeError):
        return "unknown", str(exc)

    # Lazy-load SDK exception types
    _load_sdk_exceptions()

    # Handle ProcessError — the most common error from the SDK subprocess.
    # It has exit_code and stderr attributes that we can use to classify
    # the error and give the user actionable feedback instead of generic text.
    if _ProcessError is not None and isinstance(exc, _ProcessError):
        exit_code = getattr(exc, "exit_code", None)
        stderr = getattr(exc, "stderr", "") or ""
        stderr_lower = stderr.lower()

        logger.error(
            "claude_process_error",
            exit_code=exit_code,
            stderr=stderr[:500] if stderr else "(empty)",
        )

        if stderr:
            # Try to classify based on stderr content
            if any(kw in stderr_lower for kw in ("rate limit", "429", "too many requests")):
                return "rate_limit", _format_user_error("rate_limit", stderr)
            if any(kw in stderr_lower for kw in ("unauthorized", "401", "403", "invalid api", "not logged in", "please run /login", "oauth token")):
                return "auth", _format_user_error("auth", stderr)
            if any(kw in stderr_lower for kw in ("context length", "context window", "token limit")):
                return "context_overflow", _format_user_error("context_overflow", stderr)

        # Fallback: if stderr is empty or unclassified, give guidance
        if exit_code == 1 and (not stderr or stderr.strip() == "Check stderr output for details"):
            return "stall", (
                "Claude CLI stopped responding during generation. "
                "This can happen due to API errors or network issues. "
                "Try sending your message again."
            )
        if stderr:
            return "unknown", _format_user_error("unknown", stderr)
        return "unknown", (
            f"Claude CLI exited with code {exit_code}. "
            "Try sending your message again."
        )

    # Handle CLI connection errors
    if _CLIConnectionError is not None and isinstance(exc, _CLIConnectionError):
        # Несуществующий cwd: SDK кидает CLIConnectionError с текстом «Working
        # directory does not exist: <путь>». Раньше это маскировалось под «CLI
        # не найден» (claude на месте, проблема в пути проекта). Даём точную
        # причину, чтобы пользователь правил путь проекта, а не искал claude.
        if "working directory does not exist" in str(exc).lower():
            return "cwd_missing", (
                f"Папка проекта не найдена на сервере: {str(exc).split(':', 1)[-1].strip()}. "
                "Проверьте путь проекта — возможно, он настроен для другой машины."
            )
        cli_path = getattr(exc, "cli_path", None)
        details = f" (looked at: {cli_path})" if cli_path else ""
        return "unknown", (
            f"Claude Code CLI not found{details}. "
            "Make sure Claude is installed and in PATH."
        )

    exc_str = str(exc).lower()
    if any(kw in exc_str for kw in ("rate limit", "rate_limit", "429", "too many requests")):
        return "rate_limit", str(exc)
    if any(kw in exc_str for kw in ("context length", "context window", "token limit")):
        return "context_overflow", str(exc)
    if any(kw in exc_str for kw in ("unauthorized", "401", "403", "forbidden", "not logged in", "please run /login", "oauth token")):
        return "auth", str(exc)
    if any(kw in exc_str for kw in ("timeout", "timed out", "deadline")):
        return "timeout", str(exc)
    if any(kw in exc_str for kw in ("stall", "waiting_input", "process_error")):
        return "stall", str(exc)
    return "unknown", str(exc)


# Patterns to detect transient (retryable) errors
_TRANSIENT_PATTERNS = ("rate limit", "429", "too many requests", "stall", "timed out")


def _is_transient_error(error_type: str, message: str) -> bool:
    """Check if an error is transient and worth retrying."""
    if error_type in ("rate_limit", "stall", "timeout"):
        return True
    msg_lower = message.lower()
    return any(p in msg_lower for p in _TRANSIENT_PATTERNS)


# Keywords to strip from stderr before showing to user — these are
# internal SDK plumbing messages that aren't helpful for end users.
_STDERR_CLEANUP_PREFIXES = (
    "fatal error",
    "panic",
    "runtime error",
    "goroutine",
)


def _format_user_error(error_type: str, stderr: str) -> str:
    """Format SDK stderr into a user-friendly message."""
    lines = stderr.strip().splitlines()

    # Find the first meaningful line (skip SDK internals)
    meaningful = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.lower().startswith(_STDERR_CLEANUP_PREFIXES):
            meaningful.append(stripped)

    detail = meaningful[0] if meaningful else "(no details)"
    # Truncate very long lines
    if len(detail) > 300:
        detail = detail[:297] + "..."

    messages = {
        "rate_limit": f"Rate limit reached. Please wait a moment and try again. ({detail})",
        "auth": f"Authentication error. Please check your Anthropic API key. ({detail})",
        "context_overflow": "Context window exceeded. Try starting a new session or reducing context.",
        "unknown": f"Claude CLI error: {detail}",
    }
    return messages.get(error_type, f"Error: {detail}")


# Маркеры «Claude разлогинен / OAuth-токен протух». CLI печатает их как ОБЫЧНЫЙ
# вывод (не исключение), поэтому _classify_error их не ловит — и «Not logged in»
# утекал в чат как нормальный ответ: снаружи не отличить «сервис упал» от «жив,
# но авторизация слетела». Ловим маркер → отдаём явную ошибку авторизации +
# громкий лог (журнал/алертинг), чтобы админ сразу знал, что нужен повторный
# вход служебного пользователя (токен рано или поздно протухает у всех).
_AUTH_EXPIRED_MARKERS = (
    "please run /login",
    "not logged in",
    "invalid api key",
    "oauth token has expired",
    "oauth token expired",
    "run `/login`",
    "login to authenticate",
)
AUTH_EXPIRED_USER_MSG = (
    "🔒 Claude-авторизация на сервере истекла (OAuth-токен протух). Это НЕ сбой "
    "платформы — нужен повторный вход служебного пользователя: на сервере "
    "выполнить `claude /login` (либо `/login` в Claude CLI) и перезапустить "
    "сервис. До этого ответы недоступны."
)


def _looks_like_auth_expired(text: str | None) -> bool:
    """True, если короткий вывод Claude — это маркер «не залогинен/токен протух».

    Ограничиваем длину: auth-сообщение CLI краткое, поэтому длинный ответ, где
    Claude легитимно упоминает /login, ложно не сработает.
    """
    if not text:
        return False
    t = text.strip().lower()
    if len(t) > 400:
        return False
    return any(m in t for m in _AUTH_EXPIRED_MARKERS)



# Контрольные символы для оборачивания thinking-блоков в потоке текста.
# Контракт распарсивают: src/utils/formatter.py (Telegram-формат), и
# web/src/lib/stripThinking.ts (Web UI). При изменении — обновить ВСЕ ТРИ.
THINKING_OPEN_MARKER = "\x01THINKING\x02"
THINKING_CLOSE_MARKER = "\x02THINKING\x01"

# Удаляет thinking-блоки (закрытые и хвостовой незакрытый) из буфера текста —
# чтобы отличить «был ли ВИДИМЫЙ ответ» от «было только мышление». Зеркалит
# web/src/lib/stripThinking.ts.
_THINKING_BLOCK_RE = re.compile(
    re.escape(THINKING_OPEN_MARKER) + r".*?" + re.escape(THINKING_CLOSE_MARKER),
    re.DOTALL,
)
_THINKING_OPEN_TAIL_RE = re.compile(
    re.escape(THINKING_OPEN_MARKER) + r".*$", re.DOTALL
)


def _visible_text(buf: str) -> str:
    """Текст без thinking-блоков (видимый ответ)."""
    if not buf:
        return ""
    return _THINKING_OPEN_TAIL_RE.sub("", _THINKING_BLOCK_RE.sub("", buf))

# Инструменты, запрещённые в режиме «только чтение» (доступ readonly):
# Claude может читать (Read/Grep/Glob), но не менять файлы и не выполнять
# мутирующие команды.
READONLY_DISALLOWED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash")


class ClaudeEventType(Enum):
    LOG = auto()
    TEXT = auto()
    ERROR = auto()
    TOOL_USE = auto()
    TOOL_INPUT = auto()
    SUBAGENT_START = auto()
    SUBAGENT_LOG = auto()
    SUBAGENT_FINISH = auto()
    INIT = auto()
    COMPLETE = auto()
    USAGE = auto()
    THINKING_DELTA = auto()


@dataclass
class ClaudeEvent:
    type: ClaudeEventType
    content: str = ""
    metadata: dict[str, Any] | None = None


@dataclass
class ClaudeImageAttachment:
    source_path: str
    file_name: str
    mime_type: str
    base64_data: str
    kind: str = "image"


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def update_from_metadata(self, meta: dict[str, Any]) -> None:
        """Update usage from a result/usage metadata dict."""
        raw = meta.get("usage", meta)
        usage = _normalize_usage(raw) if raw is not meta else meta
        if not isinstance(usage, dict):
            return
        self.input_tokens = usage.get("input_tokens", 0)
        self.output_tokens = usage.get("output_tokens", 0)
        self.cache_read_tokens = usage.get(
            "cache_read_input_tokens", usage.get("cache_read_tokens", 0)
        )
        self.cache_creation_tokens = usage.get(
            "cache_creation_input_tokens", usage.get("cache_creation_tokens", 0)
        )
        self.cost_usd = meta.get("cost_usd", 0.0)

    def to_dict(self) -> dict[str, Any]:
        """Return usage as a dict for event metadata."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass
class _SDKEventState:
    current_tool_name: str = ""
    current_tool_json: str = ""
    had_tool_use: bool = False
    had_text_output: bool = False
    post_tool_buffer: str = ""
    # True, как только пришёл хоть один partial-`stream_event` с контентом
    # (text_delta / thinking / tool_use). Тогда финальный собранный
    # `AssistantMessage` — ДУБЛИКАТ уже стримленного и его надо пропускать,
    # иначе текст/инструменты задвоятся (см. _events_from_sdk_message).
    saw_stream_delta: bool = False
    # Внутри ли открытого thinking-блока. При partial-стриме мышление приходит
    # пачкой thinking_delta. ВАЖНО: SDK интерливит thinking_delta пустыми
    # SystemMessage (одно на дельту) → они становятся не-text событиями и
    # форсируют флаш текстового буфера в релае, ОТРЫВАЯ маркер OPEN в отдельный
    # чанк/бабл и ломая пару OPEN…CLOSE на фронте (сырые «мысли» утекают). Чтобы
    # это исключить, мышление КОПИМ в thinking_buffer и отдаём ОДНИМ атомарным
    # событием OPEN+буфер+CLOSE на закрытии блока — маркеры всегда смежны.
    in_thinking: bool = False
    thinking_buffer: str = ""


@dataclass
class _ActiveSDKRun:
    worker_task: asyncio.Task[None]
    stop_requested: bool = False
    pid: int | None = None
    returncode: int | None = None


@dataclass
class _ActiveTmuxRun:
    session_name: str
    stop_requested: bool = False
    pid: str | None = None
    returncode: int | None = None


@dataclass
class _SmartMonitorIssue:
    kind: str
    pattern: str


@dataclass
class _CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def _load_sdk() -> tuple[type[Any], Any]:
    """Import the Claude Python SDK only when needed."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions, query
    except ImportError as exc:
        raise RuntimeError(
            "claude-agent-sdk is not installed in this Python environment"
        ) from exc

    return ClaudeAgentOptions, query


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_usage(obj: Any) -> dict[str, Any]:
    """Convert SDK Usage object or dict to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    # SDK Usage object — extract attributes
    return {
        "input_tokens": getattr(obj, "input_tokens", 0) or 0,
        "output_tokens": getattr(obj, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(obj, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(obj, "cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens": getattr(obj, "cache_read_tokens", 0) or 0,
        "cache_creation_tokens": getattr(obj, "cache_creation_tokens", 0) or 0,
    }


def _type_name(obj: Any) -> str:
    return str(_value(obj, "type", "") or type(obj).__name__)


def _format_tool_params(name: str, params: dict[str, Any]) -> str:
    """Format tool parameters into a clean, readable string."""
    lines = [f"🔧 {name}"]

    if name in ("Read", "read"):
        fp = params.get("file_path", "")
        if fp:
            parts = fp.rsplit("/", 2)
            short = "/".join(parts[-2:]) if len(parts) >= 2 else fp
            lines.append(f"   📂 {short}")

    elif name in ("Edit", "edit", "MultiEdit"):
        fp = params.get("file_path", "")
        if fp:
            parts = fp.rsplit("/", 2)
            short = "/".join(parts[-2:]) if len(parts) >= 2 else fp
            lines.append(f"   📂 {short}")
        old = params.get("old_string", "")
        new = params.get("new_string", "")
        if old and new:
            old_preview = old.strip()[:40].replace("\n", " ")
            new_preview = new.strip()[:40].replace("\n", " ")
            lines.append(f"   ✏️ {old_preview}")
            lines.append(f"   → {new_preview}")

    elif name in ("Write", "write"):
        fp = params.get("file_path", "")
        if fp:
            parts = fp.rsplit("/", 2)
            short = "/".join(parts[-2:]) if len(parts) >= 2 else fp
            lines.append(f"   📝 {short}")

    elif name in ("Bash", "bash"):
        cmd = params.get("command", "")
        desc = params.get("description", "")
        if desc:
            lines.append(f"   💬 {desc[:60]}")
        elif cmd:
            cmd_preview = cmd.strip()[:60].replace("\n", " ")
            lines.append(f"   $ {cmd_preview}")

    elif name in ("Search", "Grep", "search", "grep", "Glob", "glob"):
        pattern = params.get("pattern", params.get("query", ""))
        path = params.get("path", params.get("directory", ""))
        if pattern:
            lines.append(f"   🔍 {pattern[:40]}")
        if path:
            parts = path.rsplit("/", 2)
            short = "/".join(parts[-2:]) if len(parts) >= 2 else path
            lines.append(f"   📂 {short}")

    elif name in ("LS", "ls", "ListDir"):
        path = params.get("path", params.get("directory", ""))
        if path:
            parts = path.rsplit("/", 2)
            short = "/".join(parts[-2:]) if len(parts) >= 2 else path
            lines.append(f"   📂 {short}")

    elif name in ("AskUserQuestion", "ask_user_question"):
        # Структурированные вопросы рендерит фронт по metadata.questions —
        # тут только короткий human-readable preview для Telegram.
        questions = params.get("questions") or []
        if isinstance(questions, list) and questions:
            first = questions[0] if isinstance(questions[0], dict) else {}
            q_text = str(first.get("question", "")).strip()
            if q_text:
                preview = q_text[:80] + ("…" if len(q_text) > 80 else "")
                lines.append(f"   ❓ {preview}")

    else:
        for key, val in list(params.items())[:3]:
            val_str = str(val)[:50].replace("\n", " ")
            lines.append(f"   {key}: {val_str}")

    return "\n".join(lines)


class ClaudeBridge:
    """Bridge between Telegram handlers and Claude transports."""

    def __init__(
        self,
        permission_mode: str = "bypassPermissions",
        timeout_minutes: int = 30,
        max_turns: int = 100,
        idle_timeout_minutes: int = 30,
        idle_timeout_seconds: float | None = None,
        transport: Literal["sdk", "tmux"] = "sdk",
        tmux_session_prefix: str = "claude_topic",
        capture_poll_interval_seconds: float = 0.2,
        capture_history_lines: int = 2000,
        max_retries: int = 2,
        extended_thinking: bool = True,
        scratch_dir: str | Path | None = None,
        web_public_origin: str | None = None,
        require_jail: bool = False,
    ):
        self.permission_mode = permission_mode
        self.extended_thinking = extended_thinking
        # Fail-CLOSED политика OS-джейла: если require_jail=True, а bwrap/userns
        # недоступны, confine'нутая сессия ОТКАЗЫВАЕТСЯ стартовать (см.
        # _build_sdk_options), а не тихо запускается без изоляции. При False —
        # best-effort (соло self-host).
        self.require_jail = require_jail
        # Публичный адрес веб-платформы (если веб включён) — подмешиваем в
        # системный промпт, чтобы Claude называл его, а не внутренний
        # 127.0.0.1, когда спрашивают «адрес/ссылку» (issue #2).
        self._web_public_origin = (web_public_origin or "").strip() or None
        self.timeout_seconds = timeout_minutes * 60
        self.max_turns = max_turns
        self.max_retries = max_retries
        self.idle_timeout_seconds = (
            float(idle_timeout_seconds)
            if idle_timeout_seconds is not None
            else idle_timeout_minutes * 60
        )
        self.transport = transport
        self.tmux_session_prefix = tmux_session_prefix
        self.capture_poll_interval_seconds = capture_poll_interval_seconds
        self.capture_history_lines = capture_history_lines
        # Общий scratch-каталог для сессий БЕЗ проекта («чистый Claude»).
        # ДОЛЖЕН совпадать с тем, что использует веб (settings.get_scratch_dir),
        # иначе no-project сессии веба и Telegram-бота стартуют в разных cwd и
        # deeplink-resume не находит транскрипт (Claude хранит сессии по cwd).
        self._scratch_dir = str(scratch_dir) if scratch_dir else None
        self._session_ids: dict[int, str] = {}
        self._active_processes: dict[int, Any] = {}
        self._tmux_sessions: dict[int, str] = {}
        # Топики, чей ПЕРВЫЙ resume должен форкнуть Claude-сессию в новый
        # session_id вместо продолжения in-place. Используется deeplink
        # «Продолжить в Telegram»: веб-сессия и новый топик стартуют с одного
        # session_id, и без форка два subprocess'а дописывали бы один
        # transcript-файл. Маркер «израсходован», как только у топика появился
        # собственный session_id в ``_session_ids`` (см. ``_should_fork``);
        # пока этого не случилось (например первый прогон упал), следующий
        # resume снова форкнет от исходной базы — это безопасно.
        self._fork_pending: set[int] = set()

    async def start_cleanup_loop(self) -> None:
        """Prepare bridge state for the configured transport."""
        if self.transport != "tmux":
            try:
                _load_sdk()
                logger.info("bridge_started", transport="claude-agent-sdk")
            except RuntimeError as exc:
                logger.warning("bridge_started_without_sdk", error=str(exc))
            return

        sessions = await self._discover_tmux_sessions()
        self._tmux_sessions = sessions
        logger.info("bridge_started", transport="tmux", sessions=len(sessions))

    def restore_session_ids(self, mapping: dict[int, str]) -> None:
        """Restore topic_id -> session_id mapping from persisted storage.

        Called by core.py at startup so that ``--resume`` works for tmux
        sessions that survived a bot restart.
        """
        restored = 0
        for topic_id, session_id in mapping.items():
            if session_id and topic_id not in self._session_ids:
                self._session_ids[topic_id] = session_id
                restored += 1
        if restored:
            logger.info("session_ids_restored", count=restored)

    def clear_session_cache(self, topic_id: int) -> None:
        """Remove cached session_id for a topic (e.g. after context overflow)."""
        self._session_ids.pop(topic_id, None)
        self._fork_pending.discard(topic_id)

    def mark_fork_pending(self, topic_id: int) -> None:
        """Пометить топик для форка на первом resume (см. ``_fork_pending``).

        Вызывается deeplink-хендлером после копирования веб-``session_id`` в
        новую топик-сессию, чтобы первый прогон создал изолированный
        transcript, а не дописывал общий с веб-сессией.
        """
        self._fork_pending.add(topic_id)

    def _should_fork(self, topic_id: int, resume_sid: str | None) -> bool:
        """True, если этот resume должен форкнуть сессию.

        Форкаем только когда: (1) есть что резюмировать, (2) топик помечен
        ``mark_fork_pending`` и (3) у топика ещё НЕТ собственного session_id в
        кэше — то есть мы всё ещё резюмируем исходную (общую) базу. Как только
        прогон вернёт новый session_id и он осядет в ``_session_ids``, форк
        больше не нужен — дальнейшие resume идут in-place по своему sid.
        """
        return (
            bool(resume_sid)
            and topic_id in self._fork_pending
            and topic_id not in self._session_ids
        )

    def _resolve_cwd(self, project_path: str | Path) -> str:
        """Эффективный рабочий каталог Claude.

        Пустой путь или ``.`` (сессия без проекта, «чистый Claude») → общий
        scratch-каталог (``settings.get_scratch_dir`` — тот же, что у веба):
        no-project сессии веба и бота стартуют в ОДНОМ cwd, поэтому
        deeplink-resume находит транскрипт, а Claude не запускается в каталоге
        приложения. С проектом — путь проекта (expanduser). Может бросить
        OSError, если scratch не создаётся (нет прав)."""
        pp = str(Path(project_path or ".").expanduser())
        if pp == "." and self._scratch_dir:
            scratch = Path(self._scratch_dir).expanduser()
            scratch.mkdir(parents=True, exist_ok=True)
            return str(scratch.resolve())
        return pp

    def get_active_topic_pids(self) -> dict[int, int]:
        """Return {topic_id: pid} for currently running processes."""
        result: dict[int, int] = {}
        for tid, proc in self._active_processes.items():
            pid = getattr(proc, "pid", None) or getattr(proc, "process", {})
            if isinstance(pid, int):
                result[tid] = pid
            else:
                inner = getattr(proc, "process", None)
                if inner and hasattr(inner, "pid"):
                    result[tid] = inner.pid
        return result

    @property
    def discovered_tmux_topics(self) -> dict[int, str]:
        """Return discovered tmux topic_id -> session_name mapping."""
        return dict(self._tmux_sessions)

    async def shutdown(self) -> None:
        """Stop local bridge work without destroying tmux-backed Claude sessions."""
        if self.transport == "tmux":
            self._active_processes.clear()
            logger.info(
                "bridge_shutdown",
                transport="tmux",
                preserved_sessions=len(self._tmux_sessions),
            )
            return

        for topic_id in list(self._active_processes):
            await self.cancel_message(topic_id)

        tasks = [
            run.worker_task
            for run in self._active_processes.values()
            if isinstance(run, _ActiveSDKRun)
        ]
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._active_processes.clear()

    async def check_auth(self) -> bool:
        if self.transport != "tmux":
            try:
                options_cls, query_fn = _load_sdk()
            except RuntimeError:
                return await self._check_auth_via_cli()

            options = options_cls(
                cwd=str(Path.cwd()),
                permission_mode=self.permission_mode,
                max_turns=1,
                setting_sources=["project", "user"],
                env=self._spawn_env(),
            )

            try:
                async for raw_event in query_fn(prompt="hi", options=options):
                    for event in self._events_from_sdk_message(
                        raw_event,
                        _SDKEventState(),
                    ):
                        if event.type == ClaudeEventType.ERROR:
                            return False
                        if event.type in {
                            ClaudeEventType.TEXT,
                            ClaudeEventType.USAGE,
                            ClaudeEventType.INIT,
                        }:
                            return True
                return True
            except Exception as exc:
                logger.error("auth_check_failed", error=str(exc))
                return False

        return await self._check_auth_via_cli()

    async def send_message(
        self,
        message: str,
        topic_id: int,
        project_path: str | Path,
        session_id: str | None = None,
        attachments: list[ClaudeImageAttachment] | None = None,
        readonly: bool = False,
        confine_root: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        anthropic_api_key: str | None = None,
    ) -> AsyncIterator[ClaudeEvent]:
        # Эффективный cwd: no-project → общий scratch (тот же, что у веба), чтобы
        # deeplink-resume находил транскрипт. Несуществующая папка проекта даёт
        # понятную ошибку через _classify_error («папка не найдена», а не «CLI
        # не найден»), когда подпроцесс реально упадёт.
        try:
            project_path = self._resolve_cwd(project_path)
        except OSError as exc:
            yield ClaudeEvent(
                type=ClaudeEventType.ERROR,
                content=f"Не удалось подготовить рабочий каталог: {exc}",
                metadata={"error_type": "cwd_error"},
            )
            return

        # Fail-CLOSED (H-1): confine'нутая (непривилегированная) сессия может
        # быть защищена firewall'ом ТОЛЬКО на SDK-пути (_build_firewall_hooks).
        # На tmux/любом не-SDK транспорте навесить хук нельзя — поэтому НЕ
        # запускаем Claude без защиты, а отказываем (иначе тихий обход firewall'а).
        if confine_root and self.transport != "sdk":
            yield ClaudeEvent(
                type=ClaudeEventType.ERROR,
                content=(
                    "Сессия с ограничением доступа недоступна на текущем "
                    "транспорте Claude (защита-firewall работает только на SDK). "
                    "Обратитесь к администратору."
                ),
                metadata={"error_type": "confine_unsupported_transport"},
            )
            return

        if self.transport == "tmux":
            tmux_sid = self._session_ids.get(topic_id) or session_id
            async for event in self._send_message_via_tmux(
                message=message,
                topic_id=topic_id,
                project_path=project_path,
                session_id=tmux_sid,
                attachments=attachments,
                readonly=readonly,
                fork_session=self._should_fork(topic_id, tmux_sid),
                anthropic_api_key=anthropic_api_key,
            ):
                yield event
            if attachments:
                self._cleanup_attachments(attachments, Path(project_path).expanduser())
            return

        stored_sid = self._session_ids.get(topic_id) or session_id
        fork_session = self._should_fork(topic_id, stored_sid)
        message = self._build_prompt_with_attachments(message, attachments)

        logger.info(
            "claude_sending",
            topic_id=topic_id,
            cwd=project_path,
            session_id=stored_sid[:12] + "..." if stored_sid else None,
            msg_preview=message[:60].replace("\n", " "),
        )

        try:
            options_cls, query_fn = _load_sdk()
            options = self._build_sdk_options(
                options_cls=options_cls,
                project_path=project_path,
                session_id=stored_sid,
                readonly=readonly,
                fork_session=fork_session,
                confine_root=confine_root,
                mcp_servers=mcp_servers,
                anthropic_api_key=anthropic_api_key,
            )
        except RuntimeError as exc:
            # Fail-CLOSED: для confine'нутой сессии CLI-fallback означал бы запуск
            # БЕЗ firewall'а (он только в SDK-опциях) — отказываем, не падаем в CLI.
            if confine_root:
                logger.warning(
                    "confine_sdk_unavailable_refusing", error=str(exc), topic_id=topic_id
                )
                yield ClaudeEvent(
                    type=ClaudeEventType.ERROR,
                    content=(
                        "Не удалось инициализировать защищённый режим (SDK "
                        "недоступен); сессия с ограничением доступа не запущена."
                    ),
                    metadata={"error_type": "confine_sdk_unavailable"},
                )
                return
            logger.warning(
                "sdk_unavailable_falling_back_to_cli",
                error=str(exc),
                topic_id=topic_id,
            )
            async for event in self._send_message_via_cli(
                message=message,
                topic_id=topic_id,
                project_path=project_path,
                session_id=stored_sid,
                readonly=readonly,
                fork_session=fork_session,
                mcp_servers=mcp_servers,
                anthropic_api_key=anthropic_api_key,
            ):
                yield event
            if attachments:
                self._cleanup_attachments(attachments, Path(project_path).expanduser())
            return
        except Exception as exc:
            error_type, error_msg = _classify_error(exc)
            logger.error("send_error", error=error_msg, error_type=error_type, topic_id=topic_id)
            yield ClaudeEvent(
                type=ClaudeEventType.ERROR,
                content=error_msg,
                metadata={"error_type": error_type},
            )
            return

        usage = TokenUsage()
        text_buffer = ""
        new_session_id = stored_sid

        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                retry_delay = 2 ** attempt
                logger.warning(
                    "retrying_sdk_stream",
                    topic_id=topic_id,
                    attempt=attempt,
                    max_retries=self.max_retries,
                    delay=retry_delay,
                )
                await asyncio.sleep(retry_delay)
                # Если прошлая попытка уже форкнула и захватила новый sid,
                # ретрай должен резюмировать ЕГО in-place, а не форкать снова
                # от исходной базы (иначе плодим лишний transcript).
                if fork_session and not self._should_fork(topic_id, new_session_id):
                    fork_session = False
                    options = self._build_sdk_options(
                        options_cls=options_cls,
                        project_path=project_path,
                        session_id=new_session_id,
                        readonly=readonly,
                        fork_session=False,
                        confine_root=confine_root,
                        mcp_servers=mcp_servers,
                        anthropic_api_key=anthropic_api_key,
                    )

            queue: asyncio.Queue[ClaudeEvent | object] = asyncio.Queue()
            active_run = _ActiveSDKRun(worker_task=asyncio.create_task(asyncio.sleep(0)))
            active_run.worker_task = asyncio.create_task(
                self._consume_sdk_stream(
                    topic_id=topic_id,
                    prompt=message,
                    options=options,
                    query_fn=query_fn,
                    queue=queue,
                    active_run=active_run,
                )
            )
            self._active_processes[topic_id] = active_run

            try:
                retry_needed = False
                # Был ли УЖЕ отдан контент (видимый текст/инструмент) в этой
                # попытке: если да — ретрай переслал бы его повторно (дубль в
                # вебе/истории), поэтому ретраим только пока ничего не утекло.
                emitted_content = False
                while True:
                    item = await queue.get()
                    if item is _STREAM_DONE:
                        break

                    event = item
                    assert isinstance(event, ClaudeEvent)

                    if event.type == ClaudeEventType.INIT and event.metadata:
                        sid = event.metadata.get("session_id")
                        if sid:
                            new_session_id = sid
                            self._session_ids[topic_id] = sid

                    if event.type == ClaudeEventType.TEXT:
                        text_buffer += event.content
                        if event.content:
                            emitted_content = True
                        sid = (event.metadata or {}).get("session_id")
                        if sid:
                            new_session_id = sid
                            self._session_ids[topic_id] = sid

                    if event.type == ClaudeEventType.TOOL_USE:
                        emitted_content = True

                    if event.type == ClaudeEventType.USAGE and event.metadata:
                        usage.update_from_metadata(event.metadata)
                        usage.cost_usd = event.metadata.get("total_cost_usd", usage.cost_usd)

                        # Фолбэк по ВИДИМОМУ тексту: если ответ не пришёл
                        # дельтами (например, стримилось только мышление) —
                        # берём готовый result, иначе видимый ответ потерялся бы.
                        result_text = event.metadata.get("result", "")
                        if result_text and not _visible_text(text_buffer).strip():
                            text_buffer += result_text
                            yield ClaudeEvent(type=ClaudeEventType.TEXT, content=result_text)

                        sid = event.metadata.get("session_id")
                        if sid:
                            new_session_id = sid
                            self._session_ids[topic_id] = sid

                    if event.type == ClaudeEventType.ERROR:
                        error_type = (event.metadata or {}).get("error_type", "unknown")
                        if error_type in ("stall", "timeout", "connection", "rate_limit"):
                            # Не ретраим, если контент уже частично ушёл —
                            # повтор задвоил бы текст на вебе (issue из аудита).
                            if attempt < self.max_retries and not emitted_content:
                                retry_needed = True
                                logger.warning(
                                    "transient_error_retrying",
                                    topic_id=topic_id,
                                    error_type=error_type,
                                    attempt=attempt + 1,
                                    max_retries=self.max_retries,
                                )
                            else:
                                yield event
                        else:
                            yield event

                    if retry_needed:
                        # Reset text buffer since we'll re-stream on retry
                        text_buffer = ""
                        break

                    yield event
            finally:
                # Если генератор закрывают/отменяют (web stop_generation →
                # task.cancel() → GeneratorExit в этом async-генераторе)
                # ДО завершения стрима — worker_task всё ещё крутит SDK-запрос
                # и `await` на нём подвесил бы нас навсегда. Поэтому при
                # незавершённом worker'е сперва отменяем его. Отмена
                # доводит worker до его обработчика CancelledError, где SDK
                # async-iterator закрывается через aclose() — что синхронно
                # гасит дочерний процесс Claude (terminate/kill), не оставляя
                # осиротевшего `claude` (см. _consume_sdk_stream).
                if not active_run.worker_task.done():
                    active_run.stop_requested = True
                    active_run.worker_task.cancel()
                try:
                    await active_run.worker_task
                except asyncio.CancelledError:
                    pass
                self._active_processes.pop(topic_id, None)

            if retry_needed:
                continue

            # Claude дочитал вложения к моменту завершения потока — удаляем
            # временные web-upload файлы, чтобы они не копились в проекте
            # (CR3-22). При ошибке/отмене файлы остаются — это редкий путь.
            if attachments:
                self._cleanup_attachments(attachments, Path(project_path).expanduser())
            yield ClaudeEvent(
                type=ClaudeEventType.COMPLETE,
                content=text_buffer,
                metadata={
                    "session_id": new_session_id,
                    "usage": usage.to_dict(),
                },
            )
            return

    async def cancel_message(self, topic_id: int) -> bool:
        """Cancel an active request."""
        if self.transport == "tmux":
            active_run = self._active_processes.get(topic_id)
            session_name = None
            if isinstance(active_run, _ActiveTmuxRun):
                active_run.stop_requested = True
                session_name = active_run.session_name
            else:
                session_name = getattr(active_run, "session_name", None) or self._tmux_sessions.get(topic_id)

            if not session_name:
                return False

            result = await self._run_tmux("kill-session", "-t", session_name, check=False)
            if result.returncode != 0 and not isinstance(active_run, _ActiveTmuxRun):
                return False

            if isinstance(active_run, _ActiveTmuxRun):
                active_run.returncode = -15
            self._active_processes.pop(topic_id, None)
            self._tmux_sessions.pop(topic_id, None)
            return True

        active_run = self._active_processes.get(topic_id)
        if not active_run or getattr(active_run, "returncode", None) is not None:
            return False

        logger.info("cancelling_sdk_run", topic_id=topic_id)
        if isinstance(active_run, _ActiveSDKRun):
            active_run.stop_requested = True
            active_run.worker_task.cancel()
            return True

        try:
            active_run.terminate()
            return True
        except Exception as exc:
            logger.warning("cancel_failed", topic_id=topic_id, error=str(exc))
            return False

    async def close_session(self, topic_id: int) -> str | None:
        if self.transport == "tmux":
            session_name = self._tmux_sessions.pop(
                topic_id,
                self._tmux_session_name(topic_id),
            )
            await self._run_tmux("kill-session", "-t", session_name, check=False)
            self._active_processes.pop(topic_id, None)
            self._fork_pending.discard(topic_id)
            return self._session_ids.pop(topic_id, None)

        await self.cancel_message(topic_id)
        self._fork_pending.discard(topic_id)
        return self._session_ids.pop(topic_id, None)

    async def _send_message_via_tmux(
        self,
        *,
        message: str,
        topic_id: int,
        project_path: str,
        session_id: str | None,
        attachments: list[ClaudeImageAttachment] | None,
        readonly: bool = False,
        fork_session: bool = False,
        anthropic_api_key: str | None = None,
    ) -> AsyncIterator[ClaudeEvent]:
        # Fail-CLOSED (SP2/FIX-C): режим USER_KEY (у сессии есть свой ключ
        # пользователя) на tmux-транспорте НЕ поддержан. Claude здесь живёт в
        # общем долгоживущем tmux-пане, чьё окружение наследуется от tmux-сервера
        # (а не собирается через _spawn_env per-message). Подсунуть per-message
        # секрет в такой пане без утечки нельзя: он либо всплыл бы эхом в
        # capture-pane (мы стримим содержимое пана), либо осел бы в env tmux-
        # сервера (tmux show-environment). Поэтому вместо тихого запуска под
        # owner-кредами (это биллило бы владельца, а НЕ пользователя) — отказ.
        if anthropic_api_key:
            yield ClaudeEvent(
                type=ClaudeEventType.ERROR,
                content=(
                    "Персональный API-ключ недоступен на текущем транспорте "
                    "Claude (поддерживается только на SDK). Обратитесь к "
                    "администратору."
                ),
                metadata={"error_type": "user_key_unsupported_transport"},
            )
            return
        session_name = self._tmux_sessions.get(
            topic_id,
            self._tmux_session_name(topic_id),
        )
        self._tmux_sessions[topic_id] = session_name
        message = self._build_prompt_with_attachments(message, attachments)

        await self._ensure_tmux_session(session_name, project_path)
        baseline = await self._run_tmux(
            "capture-pane",
            "-pt",
            session_name,
            "-S",
            f"-{self.capture_history_lines}",
            check=False,
        )

        command = self._build_cli_command_string(
            message=message,
            session_id=session_id,
            readonly=readonly,
            fork_session=fork_session,
        )
        await self._run_tmux("send-keys", "-t", session_name, "-l", command)
        await self._run_tmux("send-keys", "-t", session_name, "C-m")

        usage = TokenUsage()
        text_buffer = ""
        new_session_id = session_id
        last_snapshot = baseline.stdout
        pending_fragment = ""
        last_progress = asyncio.get_running_loop().time()
        active_run = _ActiveTmuxRun(
            session_name=session_name,
            pid=f"tmux:{session_name}",
        )
        self._active_processes[topic_id] = active_run

        try:
            completed = False
            while True:
                captured = await self._run_tmux(
                    "capture-pane",
                    "-pt",
                    session_name,
                    "-S",
                    f"-{self.capture_history_lines}",
                    check=False,
                )

                if captured.returncode != 0:
                    if active_run.stop_requested:
                        break
                    yield ClaudeEvent(
                        type=ClaudeEventType.ERROR,
                        content=captured.stderr or f"tmux session {session_name} is unavailable",
                    )
                    return

                snapshot = captured.stdout
                delta = self._extract_capture_delta(last_snapshot, snapshot)
                if delta:
                    last_progress = asyncio.get_running_loop().time()
                    last_snapshot = snapshot
                    pending_fragment, lines = self._split_capture_lines(
                        pending_fragment,
                        delta,
                    )

                    for line in lines:
                        event = self._parse_line(line)
                        if not event:
                            continue

                        if event.type == ClaudeEventType.INIT and event.metadata:
                            sid = event.metadata.get("session_id")
                            if sid:
                                new_session_id = sid
                                self._session_ids[topic_id] = sid

                        if event.type == ClaudeEventType.TEXT:
                            text_buffer += event.content
                            sid = (event.metadata or {}).get("session_id")
                            if sid:
                                new_session_id = sid
                                self._session_ids[topic_id] = sid

                        if event.type == ClaudeEventType.USAGE and event.metadata:
                            usage.update_from_metadata(event.metadata)
                            usage.cost_usd = float(
                                event.metadata.get("total_cost_usd", usage.cost_usd)
                                or usage.cost_usd
                            )
                            sid = event.metadata.get("session_id")
                            if sid:
                                new_session_id = sid
                                self._session_ids[topic_id] = sid

                            result_text = event.metadata.get("result", "")
                            if result_text and not _visible_text(text_buffer).strip():
                                text_buffer += result_text
                                yield ClaudeEvent(type=ClaudeEventType.TEXT, content=result_text)

                        yield event

                        if event.type == ClaudeEventType.USAGE:
                            active_run.returncode = 0
                            completed = True
                            break
                    if completed:
                        break
                else:
                    issue = self._detect_smart_monitor_issue(snapshot)
                    if issue and (
                        asyncio.get_running_loop().time() - last_progress
                        >= self.idle_timeout_seconds
                    ):
                        active_run.stop_requested = True
                        active_run.returncode = -15
                        await self._run_tmux("kill-session", "-t", session_name, check=False)
                        self._tmux_sessions.pop(topic_id, None)
                        yield ClaudeEvent(
                            type=ClaudeEventType.ERROR,
                            content=f"Claude stalled waiting for terminal interaction: {issue.pattern}",
                            metadata={"error_type": "stall", "smart_monitor": issue.kind},
                        )
                        return

                await asyncio.sleep(self.capture_poll_interval_seconds)
        finally:
            self._active_processes.pop(topic_id, None)

        yield ClaudeEvent(
            type=ClaudeEventType.COMPLETE,
            content=text_buffer,
            metadata={
                "session_id": new_session_id,
                "usage": usage.to_dict(),
            },
        )

    async def _send_message_via_cli(
        self,
        *,
        message: str,
        topic_id: int,
        project_path: str,
        session_id: str | None,
        readonly: bool = False,
        fork_session: bool = False,
        mcp_servers: dict[str, Any] | None = None,
        anthropic_api_key: str | None = None,
    ) -> AsyncIterator[ClaudeEvent]:
        # Temp file with DECRYPTED per-user secrets — deleted in `finally` below.
        # Defined before the try so it's in scope for cleanup. Never logged.
        mcp_config_path = self._write_temp_mcp_config(mcp_servers)
        cmd = self._build_cli_args(
            message=message,
            session_id=session_id,
            readonly=readonly,
            fork_session=fork_session,
            mcp_config_path=mcp_config_path,
        )

        process: subprocess.Popen[bytes] | None = None
        master_fd: int | None = None
        slave_fd: int | None = None
        text_buffer = ""
        usage = TokenUsage()
        new_session_id = session_id
        raw_output: list[str] = []
        terminal_buffer = TerminalOutputBuffer()
        loop = asyncio.get_running_loop()

        async def handle_line(line: str) -> AsyncIterator[ClaudeEvent]:
            nonlocal text_buffer, new_session_id, usage

            if not line:
                return

            raw_output.append(line)
            event = self._parse_line(line)
            if not event:
                return

            if event.type == ClaudeEventType.INIT and event.metadata:
                sid = event.metadata.get("session_id")
                if sid:
                    new_session_id = sid
                    self._session_ids[topic_id] = sid

            if event.type == ClaudeEventType.TEXT:
                text_buffer += event.content
                sid = (event.metadata or {}).get("session_id")
                if sid:
                    new_session_id = sid
                    self._session_ids[topic_id] = sid

            if event.type == ClaudeEventType.USAGE and event.metadata:
                usage.update_from_metadata(event.metadata)
                usage.cost_usd = float(
                    event.metadata.get("total_cost_usd", usage.cost_usd)
                    or usage.cost_usd
                )
                sid = event.metadata.get("session_id")
                if sid:
                    new_session_id = sid
                    self._session_ids[topic_id] = sid

            yield event

        try:
            master_fd, slave_fd = os.openpty()
            process = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=project_path,
                # Инъекция per-user ANTHROPIC_API_KEY (режим USER_KEY) и на
                # CLI-fallback-пути тоже — иначе USER_KEY-сессия, упавшая в CLI
                # (SDK недоступен), тихо биллила бы owner-креды вместо ключа юзера.
                env=self._spawn_env(anthropic_api_key=anthropic_api_key),
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = None
            self._active_processes[topic_id] = process

            while True:
                try:
                    chunk = await asyncio.wait_for(
                        loop.run_in_executor(None, self._read_pty_chunk, master_fd),
                        timeout=self.idle_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    recent_output = raw_output[-9:]
                    current_line = terminal_buffer.snapshot()
                    if current_line:
                        recent_output.append(current_line)
                    issue = self._detect_smart_monitor_issue("\n".join(recent_output))
                    if issue:
                        process.kill()
                        await loop.run_in_executor(None, process.wait)
                        if current_line:
                            yield ClaudeEvent(type=ClaudeEventType.LOG, content=current_line)
                        elif raw_output:
                            yield ClaudeEvent(type=ClaudeEventType.LOG, content=raw_output[-1])
                        yield ClaudeEvent(
                            type=ClaudeEventType.ERROR,
                            content=f"Claude stalled waiting for terminal interaction: {issue.pattern}",
                            metadata={"error_type": "stall", "smart_monitor": issue.kind},
                        )
                        return
                    if process.poll() is not None:
                        break
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise

                if not chunk:
                    recent_output = raw_output[-9:]
                    current_line = terminal_buffer.snapshot()
                    if current_line:
                        recent_output.append(current_line)
                    issue = self._detect_smart_monitor_issue("\n".join(recent_output))
                    if issue:
                        process.kill()
                        await loop.run_in_executor(None, process.wait)
                        if current_line:
                            yield ClaudeEvent(type=ClaudeEventType.LOG, content=current_line)
                        elif raw_output:
                            yield ClaudeEvent(type=ClaudeEventType.LOG, content=raw_output[-1])
                        yield ClaudeEvent(
                            type=ClaudeEventType.ERROR,
                            content=f"Claude stalled waiting for terminal interaction: {issue.pattern}",
                            metadata={"error_type": "stall", "smart_monitor": issue.kind},
                        )
                        return
                    if process.poll() is not None:
                        break
                    continue

                for line in terminal_buffer.push(chunk.decode("utf-8", errors="replace")):
                    async for event in handle_line(line):
                        yield event

            remainder = terminal_buffer.flush()
            if remainder:
                async for event in handle_line(remainder):
                    yield event

            await loop.run_in_executor(None, process.wait)
        except Exception as exc:
            error_type, error_msg = _classify_error(exc)
            logger.error("cli_fallback_send_error", error=error_msg, error_type=error_type, topic_id=topic_id)
            yield ClaudeEvent(
                type=ClaudeEventType.ERROR,
                content=error_msg,
                metadata={"error_type": error_type},
            )
            return
        finally:
            # Если генератор закрывают/отменяют (web stop_generation) посреди
            # чтения PTY — дочерний `claude` ещё жив. Без явного kill он остаётся
            # осиротевшим процессом. Гасим, когда returncode ещё не выставлен.
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    await loop.run_in_executor(None, process.wait)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "cli_fallback_kill_failed", topic_id=topic_id, error=str(exc)
                    )
            if slave_fd is not None:
                os.close(slave_fd)
            if master_fd is not None:
                os.close(master_fd)
            self._active_processes.pop(topic_id, None)
            # Delete the temp file holding decrypted per-user MCP secrets.
            if mcp_config_path:
                try:
                    os.unlink(mcp_config_path)
                except OSError:
                    pass

        yield ClaudeEvent(
            type=ClaudeEventType.COMPLETE,
            content=text_buffer,
            metadata={
                "session_id": new_session_id,
                "usage": usage.to_dict(),
            },
        )

    def _write_temp_mcp_config(self, mcp_servers: dict[str, Any] | None) -> str | None:
        """Write per-user MCP servers to a 0600 temp file for the CLI fallback.

        Returns the path, or None if there are no servers. The file holds
        DECRYPTED secrets — caller MUST delete it after the run (done in the
        _send_message_via_cli finally block). Never logged.
        """
        if not mcp_servers:
            return None
        import tempfile
        fd, path = tempfile.mkstemp(prefix="vels-mcp-", suffix=".json")
        try:
            # POSIX only: on Windows os.fchmod exists but is a no-op on the
            # group/other bits (mode stays 0o666), so gate on os.name to avoid
            # a false sense of restriction. Prod is Linux, where this locks 0600.
            if os.name == "posix" and hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)  # restrict BEFORE writing secrets (POSIX)
            with os.fdopen(fd, "w") as fh:  # fd handed to fh; closed on exit
                json.dump({"mcpServers": mcp_servers}, fh)
        except BaseException:
            try:
                os.close(fd)  # if fdopen never took ownership
            except OSError:
                pass
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def _build_cli_args(
        self,
        *,
        message: str,
        session_id: str | None,
        readonly: bool = False,
        fork_session: bool = False,
        mcp_config_path: str | None = None,
    ) -> list[str]:
        cmd = [
            "claude",
            "-p",
            message,
            "--output-format",
            "stream-json",
            # БЕЗ --include-partial-messages: CLI/tmux — это фолбэк-транспорты,
            # где каждая строка парсится с НОВЫМ _SDKEventState (см. _parse_line),
            # поэтому дедуп собранного AssistantMessage по saw_stream_delta тут
            # не работает и partial-стрим задвоил бы текст. Реальный посимвольный
            # стрим включён только на SDK-пути (_build_sdk_options), где состояние
            # живёт весь прогон. Фолбэк отдаёт ответ цельным AssistantMessage.
            "--verbose",
            "--max-turns",
            str(self.max_turns),
            # Тот же системный промпт, что и на SDK-пути (русские размышления +
            # опц. публичный адрес) — иначе CLI-фолбэк отвечал бы на английском.
            "--append-system-prompt",
            self._system_prompt_append(),
        ]
        if mcp_config_path:
            # Per-user connections on the CLI fallback: load ONLY this file and
            # ignore global/project MCP configs (strict) — same isolation as SDK.
            cmd.extend(["--mcp-config", mcp_config_path, "--strict-mcp-config"])
        if readonly:
            # Доступ «только чтение»: запрещаем мутирующие встроенные инструменты.
            # НЕ добавляем --dangerously-skip-permissions — это и есть главный
            # рубеж: в неинтерактивном `-p`-режиме любой инструмент, требующий
            # разрешения (в т.ч. mcp__*-инструменты записи/выполнения, M-8), без
            # skip-permissions авто-отклоняется, поэтому denylist встроенных —
            # лишь дополнительный слой.
            cmd.extend(["--disallowedTools", " ".join(READONLY_DISALLOWED_TOOLS)])
        elif self.permission_mode == "bypassPermissions":
            cmd.append("--dangerously-skip-permissions")
        if session_id:
            cmd.extend(["--resume", session_id])
            if fork_session:
                # Форк: резюмировать историю, но в новый session_id.
                cmd.append("--fork-session")
        return cmd

    def _build_cli_command_string(
        self,
        *,
        message: str,
        session_id: str | None,
        readonly: bool = False,
        fork_session: bool = False,
    ) -> str:
        return " ".join(
            shlex.quote(part)
            for part in self._build_cli_args(
                message=message,
                session_id=session_id,
                readonly=readonly,
                fork_session=fork_session,
            )
        )

    # Путь к wrapper-скрипту bwrap-джейла (репо/scripts/vels-claude-jail.sh).
    _JAIL_WRAPPER = str(
        (Path(__file__).resolve().parents[2] / "scripts" / "vels-claude-jail.sh")
    )

    @staticmethod
    def _real_claude_path() -> str:
        """Путь к настоящему бинарю claude (внутрь которого джейл будет exec'ать)."""
        import shutil
        return (
            os.environ.get("VELS_REAL_CLAUDE")
            or shutil.which("claude")
            or "/usr/local/bin/claude"
        )

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def _jail_available() -> bool:
        """bwrap присутствует И unprivileged userns реально работают.

        M-8: раньше проверялись только наличие бинаря + sysctl-флаг
        ``unprivileged_userns_clone``. Но на LXC/OpenVZ и части Ubuntu 24.04
        (ограничения AppArmor на userns) bwrap ЕСТЬ, а `exec bwrap --unshare-user`
        всё равно падает — и confine'нутая сессия валилась с кодом 127. Поэтому
        дополнительно прогоняем реальную мини-пробу входа в user-namespace.
        Результат кэшируется (доступность не меняется за жизнь процесса).
        """
        import shutil
        import subprocess

        bwrap = shutil.which("bwrap")
        if not bwrap:
            return False
        try:
            p = Path("/proc/sys/kernel/unprivileged_userns_clone")
            if p.exists() and p.read_text().strip() == "0":
                return False
        except Exception:
            pass
        # Реальная проба: заходим в user+mount namespace и сразу выходим.
        try:
            r = subprocess.run(
                [bwrap, "--unshare-user", "--ro-bind", "/", "/", "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _build_firewall_hooks(self, confine_root: str | None) -> dict | None:
        """PreToolUse-hook firewall (H-1) для confine'нутых сессий.

        Возвращает hooks-конфиг для claude-agent-sdk или None. Хук срабатывает
        ДО выполнения инструмента (даже при bypassPermissions) и DENY'ит выход
        за корень проекта / доступ к секретам / деструктивные команды. Импорт
        HookMatcher ленивый + всё в try/except: проблемы самого firewall'а
        никогда не должны ломать генерацию."""
        if not confine_root:
            return None
        try:
            from claude_agent_sdk import HookMatcher
        except Exception:
            logger.warning("firewall_hook_sdk_unavailable")
            return None
        from src.claude.tool_firewall import firewall_check

        root = str(confine_root)

        def _deny(reason: str) -> dict:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        async def _pre_tool_use(input_data: Any, tool_use_id: Any, context: Any) -> dict:
            try:
                data = input_data if isinstance(input_data, dict) else {}
                tool_name = str(data.get("tool_name") or "")
                tool_input = data.get("tool_input") or {}
                reason = firewall_check(tool_name, tool_input, root)
            except Exception as exc:
                # Fail-CLOSED: ошибка самого firewall'а → DENY, не пропуск.
                logger.warning("firewall_hook_internal_error_failclosed", error=str(exc))
                return _deny(f"firewall internal error: {exc}")
            if reason:
                logger.warning("firewall_tool_blocked", reason=reason)
                return _deny(reason)
            return {}

        return {"PreToolUse": [HookMatcher(matcher="*", hooks=[_pre_tool_use])]}

    def _build_sdk_options(
        self,
        *,
        options_cls: type[Any],
        project_path: str,
        session_id: str | None,
        readonly: bool = False,
        fork_session: bool = False,
        confine_root: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        anthropic_api_key: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "cwd": project_path,
            "permission_mode": self.permission_mode,
            "max_turns": self.max_turns,
            "setting_sources": ["project", "user"],
            "env": self._spawn_env(
                confined=bool(confine_root),
                confine_root=confine_root,
                anthropic_api_key=anthropic_api_key,
            ),
            # Включаем настоящий посимвольный стрим: SDK отдаёт `stream_event`
            # с text_delta ПО МЕРЕ генерации, а не цельный ответ в конце.
            # Собранный AssistantMessage при этом дублирует контент — он
            # отбрасывается по saw_stream_delta (см. _events_from_sdk_message).
            # Состояние (_SDKEventState) на SDK-пути живёт весь прогон, поэтому
            # дедуп корректен. Дельты коалесцируются в ClaudeEventRelay, чтобы
            # не переполнять WS-очередь форвардера.
            "include_partial_messages": True,
        }
        # Системный промпт (append к пресету claude_code, без замены дефолта):
        # ВСЕГДА — вести размышления и ответы на русском; опц. — публичный адрес.
        # Общий с CLI-фолбэком текст: см. _system_prompt_append.
        kwargs["system_prompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": self._system_prompt_append(),
        }
        hooks = self._build_firewall_hooks(confine_root)
        if confine_root and not hooks:
            # Fail-CLOSED: confine'нутой сессии нужен PreToolUse-firewall. Если хук
            # не собрался (например, HookMatcher недоступен в этой сборке SDK) —
            # НЕ запускаем сессию без защиты. RuntimeError ловится в send_message
            # (ветка confine_sdk_unavailable) и превращается в ERROR-событие.
            raise RuntimeError("firewall hooks unavailable for confined session")
        if hooks:
            kwargs["hooks"] = hooks
        if confine_root:
            jail_ok = self._jail_available()
            # Fail-CLOSED: если джейл ОБЯЗАТЕЛЕН (require_jail), но bwrap/userns
            # недоступны — НЕ запускаем confine'нутую сессию без OS-изоляции.
            # RuntimeError со словом "jail" ловится в send_message (ветка
            # confine_sdk_unavailable) и превращается в ERROR-событие.
            if getattr(self, "require_jail", False) and not jail_ok:
                raise RuntimeError(
                    "OS jail required (require_jail) but bwrap/userns "
                    "unavailable — refusing confined session"
                )
            # M-8: bwrap-джейл — BEST-EFFORT второй рубеж (ФС-изоляция). Главный
            # рубеж confine'а — PreToolUse-firewall (hooks выше), он активен
            # ВСЕГДА. cli_path подменяет бинарь claude на wrapper ТОЛЬКО когда
            # bwrap/userns реально доступны: на хостах без них (LXC/OpenVZ,
            # некоторые Ubuntu 24.04 с AppArmor) безусловный wrapper падал бы с
            # `exited with code 127` на ПЕРВОМ сообщении каждого ученика. При
            # require_jail=False лучше запустить сессию с firewall'ом, чем не
            # запустить вовсе.
            if jail_ok:
                kwargs["cli_path"] = self._JAIL_WRAPPER
            else:
                logger.warning(
                    "jail_unavailable_best_effort_firewall_only",
                    hint="bwrap/userns missing — confined session runs with "
                    "PreToolUse firewall but WITHOUT filesystem jail",
                )
        if self.extended_thinking:
            # Включаем extended thinking + ЯВНО просим текст размышлений:
            # на Opus 4.7+ display по умолчанию "omitted" (только подпись),
            # поэтому без display="summarized" thinking-блоки не приходят.
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": 8000,
                "display": "summarized",
            }
        if readonly:
            kwargs["disallowed_tools"] = list(READONLY_DISALLOWED_TOOLS)
        if session_id:
            kwargs["resume"] = session_id
            if fork_session:
                # Резюмируем историю, но создаём НОВЫЙ session_id (изолированный
                # transcript), чтобы не дописывать общий файл с веб-сессией.
                kwargs["fork_session"] = True
        # Per-user connected services (Connections). Only this user's MCP servers
        # are attached to the run — full isolation. Empty/None ⇒ no change
        # (existing behaviour preserved for system callers). NOTE: mcp_servers
        # holds decrypted secrets — never log it.
        if mcp_servers:
            kwargs["mcp_servers"] = mcp_servers
        return options_cls(**kwargs)

    async def _consume_sdk_stream(
        self,
        *,
        topic_id: int,
        prompt: str,
        options: Any,
        query_fn: Any,
        queue: asyncio.Queue[ClaudeEvent | object],
        active_run: _ActiveSDKRun,
    ) -> None:
        state = _SDKEventState()
        # Держим ссылку на async-iterator SDK-запроса, чтобы детерминированно
        # закрыть его (aclose → query.close → transport.close → terminate/kill)
        # при отмене, а не полагаться на GC/atexit-бэкстоп SDK.
        aiter: Any = None

        try:
            query_result = query_fn(prompt=prompt, options=options)
            if hasattr(query_result, "__aiter__"):
                aiter = query_result.__aiter__()
                last_event_time = time.monotonic()
                while True:
                    try:
                        raw_event = await asyncio.wait_for(
                            aiter.__anext__(),
                            timeout=self.idle_timeout_seconds,
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        elapsed = time.monotonic() - last_event_time
                        logger.warning(
                            "sdk_stream_idle_timeout",
                            topic_id=topic_id,
                            idle_seconds=round(elapsed, 1),
                            timeout=self.idle_timeout_seconds,
                        )
                        await queue.put(
                            ClaudeEvent(
                                type=ClaudeEventType.ERROR,
                                content=(
                                    f"SDK stream stalled: no events for "
                                    f"{int(elapsed)}s (limit: "
                                    f"{int(self.idle_timeout_seconds)}s)"
                                ),
                                metadata={"error_type": "stall"},
                            )
                        )
                        active_run.returncode = 1
                        return

                    last_event_time = time.monotonic()
                    for event in self._events_from_sdk_message(raw_event, state):
                        await queue.put(event)
            else:
                try:
                    raw_event = await asyncio.wait_for(
                        query_result,
                        timeout=self.idle_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "sdk_query_timeout",
                        topic_id=topic_id,
                        timeout=self.idle_timeout_seconds,
                    )
                    await queue.put(
                        ClaudeEvent(
                            type=ClaudeEventType.ERROR,
                            content=(
                                f"SDK query timed out after "
                                f"{int(self.idle_timeout_seconds)}s"
                            ),
                            metadata={"error_type": "stall"},
                        )
                    )
                    active_run.returncode = 1
                    return

                for event in self._events_from_sdk_message(raw_event, state):
                    await queue.put(event)
        except asyncio.CancelledError:
            active_run.returncode = 0
            # Детерминированно закрываем SDK async-iterator: aclose() прогоняет
            # query.close() → transport.close() → terminate/kill дочернего
            # `claude` СИНХРОННО здесь, а не когда генератор соберёт GC или
            # сработает atexit-бэкстоп SDK. Так после web stop_generation не
            # остаётся осиротевшего процесса. Вторичные ошибки aclose глушим —
            # на пути отмены они не должны маскировать саму отмену.
            if aiter is not None and hasattr(aiter, "aclose"):
                try:
                    await aiter.aclose()
                except Exception:  # noqa: BLE001
                    pass
            if active_run.stop_requested:
                logger.info("sdk_run_cancelled", topic_id=topic_id)
                return
            raise
        except Exception as exc:
            active_run.returncode = 1
            error_type, error_msg = _classify_error(exc)
            logger.error(
                "send_error",
                error=error_msg,
                error_type=error_type,
                topic_id=topic_id,
                exc_class=type(exc).__name__,
                traceback=traceback.format_exc(),
            )
            await queue.put(ClaudeEvent(
                type=ClaudeEventType.ERROR,
                content=error_msg,
                metadata={"error_type": error_type},
            ))
        finally:
            # Поток мог оборваться с незакрытым thinking-блоком (нет
            # content_block_stop) — досбрасываем накопленный буфер ОДНИМ
            # атомарным OPEN+буфер+CLOSE, иначе мышление потеряется.
            flushed_think = self._flush_thinking(state)
            if flushed_think is not None:
                await queue.put(flushed_think)

            flushed_text = self._flush_post_tool_text(state)
            if flushed_text:
                await queue.put(flushed_text)

            flushed_tool = self._flush_pending_tool_event(state)
            if flushed_tool:
                await queue.put(flushed_tool)

            if active_run.returncode is None:
                active_run.returncode = 0
            await queue.put(_STREAM_DONE)

    async def _check_auth_via_cli(self) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                "hi",
                "--max-turns",
                "1",
                "--output-format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._spawn_env(),
            )
            try:
                await asyncio.wait_for(process.communicate(), timeout=20)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return False
            return process.returncode == 0
        except Exception as exc:
            logger.error("auth_check_failed", error=str(exc))
            return False

    async def _discover_tmux_sessions(self) -> dict[int, str]:
        result = await self._run_tmux("ls", "-F", "#{session_name}", check=False)
        sessions: dict[int, str] = {}
        if result.returncode != 0:
            return sessions

        prefix = f"{self.tmux_session_prefix}_"
        for line in result.stdout.splitlines():
            if not line.startswith(prefix):
                continue
            topic_part = line[len(prefix):]
            if topic_part.isdigit():
                sessions[int(topic_part)] = line
        return sessions

    def _tmux_session_name(self, topic_id: int) -> str:
        return f"{self.tmux_session_prefix}_{topic_id}"

    async def _ensure_tmux_session(self, session_name: str, project_path: str) -> None:
        result = await self._run_tmux("has-session", "-t", session_name, check=False)
        if result.returncode == 0:
            return

        await self._run_tmux(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            project_path,
        )

    async def _run_tmux(self, *args: str, check: bool = True) -> _CommandResult:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        result = _CommandResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            returncode=process.returncode or 0,
        )
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"tmux {' '.join(args)} failed")
        return result

    @staticmethod
    def _extract_capture_delta(previous: str, current: str) -> str:
        if not previous:
            return current
        if current.startswith(previous):
            return current[len(previous):]

        prefix_len = 0
        for old_char, new_char in zip(previous, current):
            if old_char != new_char:
                break
            prefix_len += 1
        return current[prefix_len:]

    @staticmethod
    def _read_pty_chunk(master_fd: int) -> bytes:
        return os.read(master_fd, 4096)

    @staticmethod
    def _split_capture_lines(prefix: str, delta: str) -> tuple[str, list[str]]:
        combined = prefix + delta
        if not combined:
            return "", []

        parts = combined.split("\n")
        if combined.endswith("\n"):
            return "", [part for part in parts if part.strip()]

        fragment = parts[-1]
        lines = [part for part in parts[:-1] if part.strip()]
        if fragment.strip() and ClaudeBridge._looks_like_complete_capture_line(fragment):
            lines.append(fragment)
            return "", lines
        return fragment, lines

    @staticmethod
    def _looks_like_complete_capture_line(fragment: str) -> bool:
        stripped = fragment.strip()
        if not stripped:
            return False
        if not stripped.startswith("{"):
            return True
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            return False
        return True

    @staticmethod
    def _log_mcp_init(mcp_servers_status: Any) -> None:
        """L-4: логируем статусы MCP-серверов из init-события.

        Диагностика: сразу видно, какие MCP подключились, а какие упали / ждут
        одобрения (needs_approval). Никогда не роняет разбор потока."""
        try:
            if not mcp_servers_status or not isinstance(mcp_servers_status, list):
                return
            summary = [
                {
                    "name": str((s or {}).get("name", "?")),
                    "status": str((s or {}).get("status", "?")),
                }
                for s in mcp_servers_status
                if isinstance(s, dict)
            ]
            if not summary:
                return
            failed = [s for s in summary if s["status"] not in ("connected", "ok")]
            logger.info("mcp_init", servers=summary)
            if failed:
                logger.warning(
                    "mcp_init_not_connected",
                    servers=failed,
                    hint="MCP-сервер не подключился/ждёт одобрения — для "
                    "project-scope включи enableAllProjectMcpServers в "
                    "~/.claude/settings.json сервис-юзера (owner-сессии)",
                )
        except Exception:
            pass

    def _events_from_sdk_message(
        self,
        raw_event: Any,
        state: _SDKEventState,
    ) -> list[ClaudeEvent]:
        if raw_event is None:
            return []

        if isinstance(raw_event, ClaudeEvent):
            return [raw_event]

        if isinstance(raw_event, str):
            return [ClaudeEvent(type=ClaudeEventType.LOG, content=raw_event)]

        event_type = _type_name(raw_event)

        if event_type in ("system", "SystemMessage"):
            subtype = _value(raw_event, "subtype", "")
            if subtype == "init":
                # L-4: init-событие несёт статус MCP-серверов
                # ([{name, status}]). Раньше брался только session_id, и было
                # ноль сигнала, что MCP не подключился / ждёт одобрения — ровно
                # та боль, из-за которой оператор не понимал, почему инструментов
                # нет. Логируем статусы и прокидываем в metadata.
                mcp_servers_status = _value(raw_event, "mcp_servers", None)
                self._log_mcp_init(mcp_servers_status)
                return [
                    ClaudeEvent(
                        type=ClaudeEventType.INIT,
                        metadata={
                            "session_id": _value(raw_event, "session_id"),
                            "mcp_servers": mcp_servers_status,
                        },
                    )
                ]
            # ВАЖНО: SDK интерливит thinking-дельты ПУСТЫМИ SystemMessage (одно
            # на дельту, .message нет → content == ""). Пустой LOG — это и
            # лишний «пустой блок» в ленте, и не-text событие, форсирующее флаш
            # текста в релае. Пустые системные сообщения отбрасываем.
            log_text = str(_value(raw_event, "message", "") or "")
            if not log_text.strip():
                return []
            return [ClaudeEvent(type=ClaudeEventType.LOG, content=log_text)]

        if event_type in ("result", "ResultMessage"):
            is_error = bool(_value(raw_event, "is_error", False))
            sid = _value(raw_event, "session_id")
            usage_dict = _normalize_usage(_value(raw_event, "usage"))
            # Claude разлогинен / токен протух: CLI кладёт «Not logged in …» в
            # result как обычный текст. Отдаём ЯВНУЮ ошибку авторизации + лог,
            # а не утекаем маркер в чат как ответ.
            result_text = str(_value(raw_event, "result", "") or "")
            if _looks_like_auth_expired(result_text):
                logger.error("claude_auth_expired", where="result", detail=result_text[:200])
                return [
                    ClaudeEvent(
                        type=ClaudeEventType.ERROR,
                        content=AUTH_EXPIRED_USER_MSG,
                        metadata={"session_id": sid, "usage": usage_dict, "error_type": "auth_expired"},
                    )
                ]
            if is_error:
                return [
                    ClaudeEvent(
                        type=ClaudeEventType.ERROR,
                        content=str(_value(raw_event, "result", "Unknown error from Claude")),
                        metadata={
                            "session_id": sid,
                            "usage": usage_dict,
                        },
                    )
                ]
            return [
                ClaudeEvent(
                    type=ClaudeEventType.USAGE,
                    metadata={
                        "session_id": sid,
                        "is_error": False,
                        "usage": usage_dict,
                        "total_cost_usd": float(_value(raw_event, "total_cost_usd", 0.0) or 0.0),
                        "result": _value(raw_event, "result", "") or "",
                    },
                )
            ]

        # ВАЖНО: SDK отдаёт РАСПАРСЕННЫЙ объект StreamEvent (класс), а не dict —
        # у него нет .type, и _type_name возвращает имя класса "StreamEvent".
        # Без этого алиаса (как у "system"/"SystemMessage" и пр.) ВСЕ partial-
        # события молча падали в конец и терялись → стрим не работал вовсе.
        if event_type in ("stream_event", "StreamEvent"):
            events = self._events_from_stream_event(raw_event, state)
            sid = _value(raw_event, "session_id")
            if sid:
                for ev in events:
                    if ev.metadata is None:
                        ev.metadata = {"session_id": sid}
                    elif "session_id" not in ev.metadata:
                        ev.metadata["session_id"] = sid
            return events

        if event_type == "subagent_event":
            parsed = self._parse_subagent_event(_value(raw_event, "event", {}) or {})
            return [parsed] if parsed else []

        if event_type in {"text_delta", "TextDelta"}:
            state.saw_stream_delta = True
            text = str(_value(raw_event, "text", "") or "")
            if text:
                state.had_text_output = True
            return [ClaudeEvent(type=ClaudeEventType.TEXT, content=text)]

        if event_type in {"tool_use", "ToolUseBlock"}:
            state.saw_stream_delta = True
            return [self._tool_use_event(raw_event)]

        if event_type in ("assistant", "AssistantMessage"):
            # Если контент уже пришёл partial-дельтами (include_partial_messages),
            # собранный AssistantMessage — точный дубль; молча отбрасываем, иначе
            # текст/инструменты/мышление задвоятся. Когда partial выключен (CLI/
            # tmux-фолбэк, check_auth) — saw_stream_delta=False и сообщение
            # обрабатывается как раньше (единственный источник контента).
            if state.saw_stream_delta:
                return []
            content = _value(raw_event, "message", {})
            body = _value(content, "content", _value(raw_event, "content", []))
            return self._events_from_content_blocks(body, state)

        text = _value(raw_event, "text")
        if isinstance(text, str) and text:
            if _looks_like_auth_expired(text):
                logger.error("claude_auth_expired", where="text", detail=text[:200])
                return [
                    ClaudeEvent(
                        type=ClaudeEventType.ERROR,
                        content=AUTH_EXPIRED_USER_MSG,
                        metadata={"error_type": "auth_expired"},
                    )
                ]
            state.had_text_output = True
            return [ClaudeEvent(type=ClaudeEventType.TEXT, content=text)]

        error_text = _value(raw_event, "error")
        if isinstance(error_text, str) and error_text:
            return [ClaudeEvent(type=ClaudeEventType.ERROR, content=error_text)]

        return []

    def _events_from_content_blocks(self, blocks: Any, state: _SDKEventState) -> list[ClaudeEvent]:
        if isinstance(blocks, str):
            return [ClaudeEvent(type=ClaudeEventType.TEXT, content=blocks)] if blocks else []
        if not isinstance(blocks, list):
            return []

        events: list[ClaudeEvent] = []
        for block in blocks:
            # Учитываем ОБЕ формы: объекты SDK (TextBlock/ToolUseBlock/...) и
            # JSON-строки из stream-json CLI/tmux ("text"/"tool_use"/"thinking").
            # Раньше брались только классы → на CLI/tmux собранное assistant-
            # сообщение давало ПУСТО (текст терялся), а multi-block резался.
            block_type = _type_name(block)
            if block_type in ("TextBlock", "text"):
                text = str(_value(block, "text", "") or "")
                if text:
                    events.append(ClaudeEvent(type=ClaudeEventType.TEXT, content=text))
            elif block_type in ("ToolUseBlock", "tool_use"):
                events.append(self._tool_use_event(block))
            elif block_type in ("ThinkingBlock", "thinking"):
                thinking_text = str(_value(block, "thinking", "") or "")
                if thinking_text:
                    # Живая дельта (одним куском — на сборочном пути мысли не
                    # инкрементальны). saw_stream_delta здесь False, поэтому
                    # двойного эмита со стрим-путём нет.
                    events.append(ClaudeEvent(type=ClaudeEventType.THINKING_DELTA, content=thinking_text))
                    events.append(ClaudeEvent(type=ClaudeEventType.TEXT, content=f"{THINKING_OPEN_MARKER}{thinking_text}{THINKING_CLOSE_MARKER}\n\n"))
        # Собранное сообщение — это маркер «не залогинен» (CLI/tmux-фолбэк, где
        # ответ приходит цельным AssistantMessage)? Не показываем как ответ, а
        # отдаём явную ошибку авторизации + лог.
        combined = "".join(e.content for e in events if e.type == ClaudeEventType.TEXT)
        if _looks_like_auth_expired(combined):
            logger.error("claude_auth_expired", where="assembled", detail=combined[:200])
            return [
                ClaudeEvent(
                    type=ClaudeEventType.ERROR,
                    content=AUTH_EXPIRED_USER_MSG,
                    metadata={"error_type": "auth_expired"},
                )
            ]
        return events

    def _events_from_stream_event(
        self,
        raw_event: Any,
        state: _SDKEventState,
    ) -> list[ClaudeEvent]:
        event = _value(raw_event, "event", {}) or {}
        event_kind = _value(event, "type", "")
        delta = _value(event, "delta", {}) or {}
        delta_type = _value(delta, "type", "")

        def _close_thinking() -> list[ClaudeEvent]:
            """Отдать накопленный thinking ОДНИМ атомарным блоком (или []),
            если thinking-блок был открыт."""
            ev = self._flush_thinking(state)
            return [ev] if ev is not None else []

        # Мышление: КОПИМ дельты в буфер и отдаём ОДНИМ атомарным OPEN+буфер+CLOSE
        # на закрытии блока (content_block_stop / старт другого блока / конец
        # потока). НЕ эмитим маркеры/текст здесь — иначе интерливленные пустые
        # SystemMessage (см. _SDKEventState.thinking_buffer) рвут пару OPEN…CLOSE
        # на отдельные баблы и сырые «мысли» утекают в видимый ответ.
        if delta_type == "thinking_delta":
            state.saw_stream_delta = True
            thinking_text = str(_value(delta, "thinking", "") or "")
            if thinking_text:
                # НЕ трогаем had_text_output: он означает ВИДИМЫЙ текст и нужен
                # для пост-тул разделителя «\n\n».
                state.thinking_buffer += thinking_text
                # Живой эфемерный канал: дельта уходит на фронт сразу. Атомарный
                # блок по-прежнему соберётся в _flush_thinking (история/«Размышления»).
                return [ClaudeEvent(type=ClaudeEventType.THINKING_DELTA, content=thinking_text)]
            return []

        if delta_type == "text_delta":
            state.saw_stream_delta = True
            prefix = _close_thinking()
            text_content = str(_value(delta, "text", "") or "")
            if state.had_tool_use and state.had_text_output and text_content:
                if not state.post_tool_buffer:
                    state.post_tool_buffer = text_content
                    return prefix + [ClaudeEvent(type=ClaudeEventType.TEXT, content="")]
                text_content = "\n\n" + state.post_tool_buffer + text_content
                state.post_tool_buffer = ""
                state.had_tool_use = False
            if text_content:
                state.had_text_output = True
            return prefix + [ClaudeEvent(type=ClaudeEventType.TEXT, content=text_content)]

        if delta_type == "input_json_delta":
            state.saw_stream_delta = True
            state.current_tool_json += str(_value(delta, "partial_json", "") or "")
            return [ClaudeEvent(type=ClaudeEventType.TOOL_INPUT, content="")]

        content_block = _value(event, "content_block", {}) or {}
        block_type = _value(content_block, "type", "")

        if block_type == "tool_use":
            state.saw_stream_delta = True
            prefix = _close_thinking()
            state.had_tool_use = True
            # Флашим ПРЕДЫДУЩИЙ tool (страховка, если его content_block_stop
            # не пришёл). Сам текущий tool отдаётся ОДНИМ событием на своём
            # content_block_stop — без раннего «плейсхолдера», иначе на вебе
            # двоились бы карточки инструмента.
            flushed = self._flush_pending_tool_event(state)
            state.current_tool_name = str(_value(content_block, "name", "tool") or "tool")
            state.current_tool_json = ""
            return prefix + ([flushed] if flushed else [])

        if block_type == "thinking":
            state.saw_stream_delta = True
            state.in_thinking = True
            # OPEN-маркер здесь НЕ эмитим — копим текст, отдадим атомарно на
            # закрытии блока (см. thinking_delta / _flush_thinking).
            thinking_text = str(_value(content_block, "thinking", "") or "")
            if thinking_text:
                state.thinking_buffer += thinking_text
                # Живой канал (как в thinking_delta); атомарный блок — в _flush_thinking.
                return [ClaudeEvent(type=ClaudeEventType.THINKING_DELTA, content=thinking_text)]
            return []

        if block_type == "text":
            # Старт видимого текстового блока — закрываем thinking, если был.
            return _close_thinking()

        if event_kind == "content_block_stop":
            # Конец блока: закрываем thinking ИЛИ отдаём накопленный tool_use
            # (полное событие с params/file_path), смотря что было открыто.
            closed = _close_thinking()
            flushed = self._flush_pending_tool_event(state)
            return closed + ([flushed] if flushed else [])

        return []

    def _system_prompt_append(self) -> str:
        """Текст-append к системному промпту (общий для SDK и CLI-фолбэка):
        ВСЕГДА просим русский (мысли Claude по умолчанию англ., а юзеру нужен
        русский и в «Размышлениях»), опц. — публичный адрес платформы (issue #2)."""
        parts = [
            "Веди свои рассуждения (extended thinking) И ответы на РУССКОМ "
            "языке — и внутренние размышления, и финальный ответ. Переходи на "
            "другой язык, только если пользователь явно об этом попросил или "
            "если того требует содержимое (код, цитаты, имена)."
        ]
        if self._web_public_origin:
            parts.append(
                f"Публичный адрес веб-интерфейса этого ассистента: "
                f"{self._web_public_origin}. Когда пользователь спрашивает "
                f"адрес/ссылку/URL веб-платформы — называй именно его, а не "
                f"внутренний bind-адрес (127.0.0.1/localhost/0.0.0.0) из конфигов."
            )
        return " ".join(parts)

    def _flush_thinking(self, state: _SDKEventState) -> ClaudeEvent | None:
        """Отдать накопленный thinking ОДНИМ атомарным событием
        ``OPEN+буфер+CLOSE`` (или None, если буфер пуст). Маркеры всегда в
        одном чанке → их не разорвать интерливленными не-text событиями
        (пустые SystemMessage), поэтому фронт гарантированно матчит пару и
        мышление сворачивается в блок «Размышления», а не утекает сырьём."""
        was_open = state.in_thinking
        state.in_thinking = False
        buf = state.thinking_buffer
        state.thinking_buffer = ""
        if not was_open and not buf:
            return None
        if buf.strip():
            return ClaudeEvent(
                type=ClaudeEventType.TEXT,
                content=f"{THINKING_OPEN_MARKER}{buf}{THINKING_CLOSE_MARKER}\n\n",
            )
        return None

    def _tool_metadata(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Метадата tool_use для фронта: имя + (file_path / bash_outputs /
        questions). Общая для собранного сообщения и для partial-стрима, иначе
        при стриме терялся file_path и веб не показывал карточку-артефакт."""
        metadata: dict[str, Any] = {"name": name}
        if not isinstance(params, dict):
            return metadata
        # Пишущие инструменты: путь файла → веб покажет карточку со скачиванием.
        if name in ("Write", "write", "Edit", "edit", "MultiEdit"):
            fp = params.get("file_path")
            if isinstance(fp, str) and fp:
                metadata["file_path"] = fp
        # Bash: вытаскиваем создаваемые файлы/папки (cp, tee, > … ) — иначе они
        # не попадали в артефакты. file_path — первый выход (ключ артефакта),
        # bash_outputs — все (их разворачивает list_tool_artifact_rows).
        if name in ("Bash", "bash"):
            cmd = params.get("command")
            if isinstance(cmd, str) and cmd:
                outs = extract_bash_outputs(cmd)
                if outs:
                    metadata["file_path"] = outs[0]
                    metadata["bash_outputs"] = outs
        # AskUserQuestion: structured questions → красивый UI с кнопками.
        if name in ("AskUserQuestion", "ask_user_question"):
            raw_questions = params.get("questions")
            if isinstance(raw_questions, list):
                clean_questions = []
                for q in raw_questions:
                    if not isinstance(q, dict):
                        continue
                    options_raw = q.get("options") or []
                    options = []
                    if isinstance(options_raw, list):
                        for opt in options_raw:
                            if isinstance(opt, dict):
                                options.append(
                                    {
                                        "label": str(opt.get("label", "")),
                                        "description": str(opt.get("description", "")),
                                    }
                                )
                            elif isinstance(opt, str):
                                options.append({"label": opt, "description": ""})
                    clean_questions.append(
                        {
                            "question": str(q.get("question", "")),
                            "header": str(q.get("header", "")),
                            "multiSelect": bool(q.get("multiSelect", False)),
                            "options": options,
                        }
                    )
                metadata["questions"] = clean_questions
        return metadata

    def _tool_use_event(self, raw_event: Any) -> ClaudeEvent:
        name = str(_value(raw_event, "name", "tool") or "tool")
        params = _value(raw_event, "input", _value(raw_event, "arguments", {})) or {}
        if not isinstance(params, dict):
            params = {}
        return ClaudeEvent(
            type=ClaudeEventType.TOOL_USE,
            content=_format_tool_params(name, params),
            metadata=self._tool_metadata(name, params),
        )

    def _flush_post_tool_text(self, state: _SDKEventState) -> ClaudeEvent | None:
        if not state.post_tool_buffer:
            return None

        sep = "\n\n" if state.had_text_output else ""
        content = sep + state.post_tool_buffer
        state.post_tool_buffer = ""
        return ClaudeEvent(type=ClaudeEventType.TEXT, content=content)

    def _flush_pending_tool_event(self, state: _SDKEventState) -> ClaudeEvent | None:
        if not state.current_tool_name:
            return None

        name = state.current_tool_name
        params: dict[str, Any] = {}
        if state.current_tool_json:
            try:
                parsed = json.loads(state.current_tool_json)
                if isinstance(parsed, dict):
                    params = parsed
            except (json.JSONDecodeError, TypeError, ValueError):
                params = {}
        formatted = _format_tool_params(name, params) if params else f"🔧 {name}"

        event = ClaudeEvent(
            type=ClaudeEventType.TOOL_USE,
            content=formatted,
            # Полная метадата (file_path/bash_outputs/questions) — как у собранного
            # сообщения, иначе при partial-стриме веб не покажет карточку-артефакт.
            metadata=self._tool_metadata(name, params),
        )
        state.current_tool_name = ""
        state.current_tool_json = ""
        return event

    def _parse_line(self, line: str) -> ClaudeEvent | None:
        if not line:
            return None

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return ClaudeEvent(type=ClaudeEventType.LOG, content=line) if line.strip() else None

        event_type = data.get("type", "")

        if event_type == "subagent_event":
            return self._parse_subagent_event(data.get("event", {}) or {})

        events = self._events_from_sdk_message(data, _SDKEventState())
        return events[0] if events else None

    def _parse_subagent_event(self, event: dict[str, Any]) -> ClaudeEvent | None:
        event_type = event.get("type", "")
        name = str(event.get("name", "Subagent") or "Subagent")

        if event_type == "start":
            return ClaudeEvent(type=ClaudeEventType.SUBAGENT_START, content=name)

        if event_type == "finish":
            return ClaudeEvent(type=ClaudeEventType.SUBAGENT_FINISH, content=name)

        if event_type == "tool_use":
            tool_event = self._tool_use_event(event)
            prefixed = "\n".join(f"> {line}" for line in tool_event.content.splitlines())
            return ClaudeEvent(type=ClaudeEventType.SUBAGENT_LOG, content=prefixed)

        text = str(event.get("text", "") or event.get("content", "") or "")
        if text:
            prefixed = "\n".join(f"> {line}" for line in text.splitlines())
            return ClaudeEvent(type=ClaudeEventType.SUBAGENT_LOG, content=prefixed)

        return None

    def _detect_smart_monitor_issue(self, text: str) -> _SmartMonitorIssue | None:
        patterns = [
            ("waiting_input", "[y/N]"),
            ("waiting_input", "Need approval to continue"),
            ("waiting_input", "Press y"),
            ("process_error", "Killed"),
            ("process_error", "segmentation fault"),
        ]
        lowered = text.lower()
        for kind, pattern in patterns:
            if pattern.lower() in lowered:
                return _SmartMonitorIssue(kind=kind, pattern=pattern)
        return None

    @staticmethod
    def _build_prompt_with_attachments(
        message: str,
        attachments: list[ClaudeImageAttachment] | None,
    ) -> str:
        """Prepend image reference instructions so Claude uses Read tool to view them."""
        if not attachments:
            return message

        image_refs = [att.source_path for att in attachments]
        if len(image_refs) == 1:
            instruction = (
                f"[Image attached: {image_refs[0]}]\n"
                f"Use the Read tool to view this image file before responding.\n\n"
            )
        else:
            paths = "\n".join(f"  - {p}" for p in image_refs)
            instruction = (
                f"[Images attached:\n{paths}\n]\n"
                f"Use the Read tool to view each image file before responding.\n\n"
            )
        return instruction + message

    def _cleanup_attachments(
        self,
        attachments: list[ClaudeImageAttachment],
        project_path: Path,
    ) -> None:
        """Remove temporary image files after Claude has processed them.

        Синхронный (только resolve/exists/unlink/rmdir) — чтобы его можно
        было безопасно звать из точек завершения async-generator
        ``send_message`` без ``await`` (await при GeneratorExit опасен).

        ``source_path`` приходит из недоверенного источника (WS-сообщение
        от веб-клиента) и резолвится строго внутри ``project_path`` — без
        этой проверки ``../../etc/passwd`` дотягивался бы до файлов
        вне проекта и ``.unlink()`` удалял бы их.
        """
        # Локальный импорт: src.utils.__init__ тянет formatter, который
        # импортирует THINKING-маркеры из этого модуля — top-level импорт
        # создал бы цикл.
        from src.utils.url_safety import is_path_within_root

        project_root = project_path.resolve()
        for att in attachments:
            candidate = project_root / att.source_path
            # Общая защита от traversal: source_path недоверенный, путь
            # обязан лежать внутри project_root, иначе .unlink() мог бы
            # удалить файл снаружи (CR3-18).
            if not is_path_within_root(project_root, candidate):
                logger.warning(
                    "attachment_cleanup_path_traversal_blocked",
                    source_path=att.source_path,
                    project_path=str(project_root),
                )
                continue
            try:
                resolved = candidate.resolve()
            except (OSError, ValueError) as e:
                logger.warning(
                    "attachment_cleanup_resolve_failed",
                    source_path=att.source_path,
                    error=str(e),
                )
                continue
            try:
                if resolved.exists():
                    resolved.unlink()
                parent = resolved.parent
                if (
                    parent != project_root
                    and parent.exists()
                    and parent.name == "images"
                    and not any(parent.iterdir())
                ):
                    parent.rmdir()
            except Exception as e:
                logger.warning("attachment_cleanup_failed", path=str(resolved), error=str(e))

    # Секреты бота, которые дочернему Claude НЕ нужны. systemd грузит .env в
    # окружение процесса, поэтому без вычистки они утекли бы в подпроцесс Claude
    # (bypassPermissions) и могли бы быть слиты через `env`/prompt-injection.
    # ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN намеренно НЕ удаляем — они
    # нужны Claude для авторизации.
    _SECRET_ENV_KEYS = (
        "TELEGRAM_BOT_TOKEN",
        "WEB_JWT_SECRET",
        "WEB_DEV_BEARER_TOKEN",
        "ADMIN_LOGIN",
        "ADMIN_PASSWORD",
    )
    # L-4: явный денилист закрывает только ИЗВЕСТНЫЕ ключи; любой будущий секрет
    # в .env утёк бы в подпроцесс Claude. Дополнительно эвристически вычищаем
    # переменные с секретообразными именами. Чистый allowlist здесь непрактичен —
    # Claude запускает Bash, которому нужно полноценное окружение (PATH/HOME/…).
    _SECRET_NAME_RE = re.compile(
        r"(SECRET|PASSWORD|PASSWD|CREDENTIAL|BEARER|PRIVATE_KEY|_TOKEN$|_API_KEY$|APIKEY)",
        re.IGNORECASE,
    )
    # Нужны самому Claude для авторизации — НЕ вычищаем, даже если имя «секретное».
    _SECRET_KEEP_KEYS = frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
        }
    )

    @staticmethod
    def _subscription_creds_present() -> bool:
        """True if a ~/.claude subscription/OAuth session exists (Claude can auth
        without the env API key). Used to decide whether it's safe to strip
        ANTHROPIC_* from a confined child's env."""
        try:
            return (Path.home() / ".claude" / ".credentials.json").exists()
        except Exception:
            return False

    @staticmethod
    def _clean_env(confined: bool = False) -> dict[str, str]:
        env = os.environ.copy()
        for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "CLAUDECODE"]:
            env.pop(key, None)
        for key in ClaudeBridge._SECRET_ENV_KEYS:
            env.pop(key, None)
        # Для confined-сессий вырезаем и креды Claude — но ТОЛЬКО если Claude
        # сможет авторизоваться через ~/.claude (иначе сломаем сам ответ).
        strip_auth = confined and ClaudeBridge._subscription_creds_present()
        for key in list(env.keys()):
            if key in ClaudeBridge._SECRET_KEEP_KEYS:
                if strip_auth:
                    env.pop(key, None)
                continue
            if ClaudeBridge._SECRET_NAME_RE.search(key):
                env.pop(key, None)
        return env

    # Ключи settings.json, которые НЕЛЬЗЯ протаскивать в confined-джейл (M-7):
    # они авто-одобряют/поднимают project-scope MCP-серверы. MCP-сервер стартует
    # subprocess при INIT сессии (для листинга tools) ДО PreToolUse-firewall,
    # поэтому вредоносный .mcp.json в проекте = RCE с правами сервис-юзера.
    _JAIL_SETTINGS_STRIP_KEYS = (
        "enableAllProjectMcpServers",
        "enabledMcpjsonServers",
        "mcpServers",
    )

    @staticmethod
    def _jail_settings_file(creds_dir: Path) -> str | None:
        """Санитизированная копия owner-``settings.json`` для монтирования в джейл.

        Реальный owner-``settings.json`` несёт ``enableAllProjectMcpServers``
        (его сидит установщик, чтобы НЕ-confined сессии владельца авто-одобряли
        project-scope MCP). Смонтировать его как есть в confined-джейл =
        авто-запуск project MCP при INIT (до firewall) → RCE (M-7). Поэтому в
        джейл монтируем КОПИЮ с вырезанными MCP-ключами; остальные предпочтения
        (model и т.п.) сохраняем. Возвращает путь к копии или None (нет исходника
        / нечитаем / не dict → в джейл не монтируем ничего, fail-safe).
        """
        src = creds_dir / "settings.json"
        try:
            if not src.exists():
                return None
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        for key in ClaudeBridge._JAIL_SETTINGS_STRIP_KEYS:
            data.pop(key, None)
        try:
            out_dir = Path.home() / ".cache" / "vels-claude"
            out_dir.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(out_dir, 0o700)
            dst = out_dir / "jail-settings.json"
            tmp = out_dir / f".jail-settings.{os.getpid()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, dst)  # атомарная подмена — без «рваных» чтений
            return str(dst)
        except Exception:
            return None

    def _spawn_env(
        self,
        confined: bool = False,
        confine_root: str | None = None,
        anthropic_api_key: str | None = None,
    ) -> dict[str, str]:
        """Окружение дочернего Claude: чистый env + жёсткий UTF-8 (чтобы
        кириллица в логах инструментов не превращалась в «?») + расширенное
        мышление (MAX_THINKING_TOKENS), если включено. Размышления Claude
        отображаются на verbose ≥ 2, но генерируются здесь.
        ``confined`` → дополнительно вырезаем ANTHROPIC_* (если есть ~/.claude).
        ``confine_root`` → добавляем env для bwrap-джейла (см. vels-claude-jail.sh).
        ``anthropic_api_key`` → ключ пользователя для инъекции (режим USER_KEY):
        подставляется ПОСЛЕ _clean_env, чтобы пережить strip, и сигналит джейлу
        не бинд-маунтить owner-креды (VELS_JAIL_NO_OWNER_CREDS=1)."""
        env = self._clean_env(confined=confined)

        # Инъекция пользовательского ключа (режим USER_KEY). Делается ПОСЛЕ
        # _clean_env — иначе confined-strip вычистил бы ANTHROPIC_API_KEY.
        # VELS_JAIL_NO_OWNER_CREDS=1 → сигнал vels-claude-jail.sh не монтировать
        # owner-креды (~/.claude/.credentials.json): у сессии есть свой ключ.
        if anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = anthropic_api_key
            env["VELS_JAIL_NO_OWNER_CREDS"] = "1"

        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        if self.extended_thinking:
            env.setdefault("MAX_THINKING_TOKENS", "8000")
        if confine_root:
            env["VELS_PROJECT_ROOT"] = str(confine_root)
            env["VELS_REAL_CLAUDE"] = self._real_claude_path()
            env.setdefault("HOME", str(Path.home()))
            creds_dir = Path(env["HOME"]) / ".claude"
            env["VELS_CLAUDE_CREDS_DIR"] = str(creds_dir)
            # M-7: в джейл монтируем НЕ сырой settings.json, а санитизированную
            # копию (без MCP-approval-ключей). jail.sh биндит её по этому пути;
            # если None — settings.json в джейле не монтируется вовсе.
            jail_settings = self._jail_settings_file(creds_dir)
            if jail_settings:
                env["VELS_JAIL_SETTINGS_FILE"] = jail_settings
        return env
