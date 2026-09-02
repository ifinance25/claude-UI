"""Security middleware and Claude tool firewall."""
from __future__ import annotations

import json
import re
import shlex
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware

import structlog

logger = structlog.get_logger()

_PROMPT_PATTERNS = (
    re.compile(r"read server environment variables", re.IGNORECASE),
    re.compile(r"ignore .*sandbox", re.IGNORECASE),
    re.compile(r"escape .*working directory", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bhistory\s+-c\b", re.IGNORECASE),
    re.compile(r"\bshutdown\b", re.IGNORECASE),
)
_COMMAND_PATTERNS = (
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bhistory\s+-c\b", re.IGNORECASE),
    re.compile(r"\bshutdown(?:\s|$)", re.IGNORECASE),
    re.compile(r"(^|/)\.env(?:\.[\w.-]+)?$", re.IGNORECASE),
    re.compile(r"(^|/)\.ssh(?:/|$)", re.IGNORECASE),
    re.compile(r"(^|/)(?:authorized_keys|id_rsa|id_ed25519)$", re.IGNORECASE),
)
_PATH_FIELDS = ("file_path", "path", "directory")

# Confined-сессии: shell-подстановка (`$VAR`/`$(...)`/backticks) обходит path-проверку
# (раскрывается в рантайме), а инлайн-интерпретаторы (`python -c`, `node -e`, …)
# исполняют произвольный код мимо firewall'а. Блокируем на сыром тексте команды.
_SHELL_SUBST_PATTERN = re.compile(r"[`$]")
_INTERPRETER_INLINE_PATTERN = re.compile(
    r"\b(?:python3?|node|deno|bun|perl|ruby|php|sh|bash|zsh|awk)\b.*?\s-(?:c|e)\b",
    re.IGNORECASE | re.DOTALL,
)
# Confined-сессии: сеть у bash общая (джейл делит сеть с Claude), поэтому денилист
# egress-команд поднимает планку против эксфильтрации/SSRF (негерметично).
# Включает bash-билтин egress /dev/tcp|/dev/udp (не требует внешней команды).
_NETWORK_CMD_PATTERN = re.compile(
    r"\b(?:curl|wget|nc|ncat|netcat|telnet|ssh|scp|sftp|ftp|rsync)\b"
    r"|/dev/(?:tcp|udp)/",
    re.IGNORECASE,
)


class SecurityViolation(RuntimeError):
    """Raised when a security rule rejects an action."""

    def __init__(self, message: str, attempted_action: str, *, category: str):
        super().__init__(message)
        self.attempted_action = attempted_action
        self.category = category


class SecurityMiddleware(BaseMiddleware):
    """Validate user input and Claude tool activity before it can do damage."""

    def __init__(
        self,
        *,
        allowed_roots: list[Path],
        audit_log_path: str | Path = "security.log",
        strict_mode: bool = True,
    ) -> None:
        self.allowed_roots = [
            Path(root).expanduser().resolve(strict=False)
            for root in allowed_roots
        ]
        self.audit_log_path = Path(audit_log_path).expanduser()
        self.strict_mode = strict_mode

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        text = getattr(event, "text", None) or getattr(event, "caption", None)
        user = getattr(event, "from_user", None)
        user_id = getattr(user, "id", None)

        if text:
            try:
                self.validate_user_message(text, user_id=user_id)
            except SecurityViolation as exc:
                logger.warning(
                    "security_prompt_blocked",
                    user_id=user_id,
                    attempted_action=exc.attempted_action,
                    category=exc.category,
                )
                if hasattr(event, "answer"):
                    await event.answer(
                        "⚠️ Сообщение заблокировано политикой безопасности.",
                    )
                if self.strict_mode:
                    return None

        return await handler(event, data)

    def validate_user_message(self, message: str, *, user_id: int | None = None) -> None:
        """Block explicit jailbreak/destructive instructions from the user."""
        for pattern in _PROMPT_PATTERNS:
            if pattern.search(message):
                self._raise_violation(
                    "Security Error: Suspicious instruction blocked.",
                    attempted_action=message,
                    category="user_input",
                    user_id=user_id,
                )

    def validate_project_path(
        self,
        project_path: str | Path,
        *,
        user_id: int | None = None,
    ) -> Path:
        """Reject Claude working directories outside configured project roots."""
        resolved = Path(project_path).expanduser().resolve(strict=False)
        if not self.allowed_roots:
            return resolved

        if any(self._is_within(resolved, root) for root in self.allowed_roots):
            return resolved

        self._raise_violation(
            "Security Error: Permission Denied. Stay in project folder.",
            attempted_action=str(project_path),
            category="project_root",
            user_id=user_id,
        )

    def validate_tool_use(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        project_root: str | Path,
        user_id: int | None = None,
    ) -> None:
        """Reject dangerous commands or file access outside the active project."""
        project_root = Path(project_root).expanduser().resolve(strict=False)
        normalized_name = tool_name.lower()

        if normalized_name == "bash":
            command = str(tool_input.get("command", ""))
            self._validate_bash_command(command, project_root=project_root, user_id=user_id)
            return

        for field in _PATH_FIELDS:
            candidate = tool_input.get(field)
            if not candidate:
                continue
            self._validate_candidate_path(
                str(candidate),
                project_root=project_root,
                attempted_action=f"{tool_name}: {candidate}",
                user_id=user_id,
            )

    def _validate_bash_command(
        self,
        command: str,
        *,
        project_root: Path,
        user_id: int | None,
    ) -> None:
        for pattern in _COMMAND_PATTERNS:
            if pattern.search(command):
                self._raise_violation(
                    "Security Error: Destructive bash command blocked.",
                    attempted_action=command,
                    category="bash_command",
                    user_id=user_id,
                )

        if _SHELL_SUBST_PATTERN.search(command):
            self._raise_violation(
                "Security Error: Shell substitution blocked.",
                attempted_action=command,
                category="shell_substitution",
                user_id=user_id,
            )

        if _INTERPRETER_INLINE_PATTERN.search(command):
            self._raise_violation(
                "Security Error: Inline interpreter execution blocked.",
                attempted_action=command,
                category="inline_interpreter",
                user_id=user_id,
            )

        if _NETWORK_CMD_PATTERN.search(command):
            self._raise_violation(
                "Security Error: Network command blocked in confined session.",
                attempted_action=command,
                category="network_command",
                user_id=user_id,
            )

        # Несбалансированные кавычки и т.п. → shlex.split бросает ValueError.
        # Нераспарсиваемую команду НЕЛЬЗЯ проверить на path-traversal, поэтому
        # блокируем её (fail-closed). Раньше ValueError всплывал мимо firewall'а
        # и приводил к fail-open (обход confine одной кавычкой).
        try:
            tokens = self._extract_path_tokens(command)
        except ValueError:
            self._raise_violation(
                "Security Error: Unparseable command blocked.",
                attempted_action=command,
                category="unparseable_command",
                user_id=user_id,
            )

        for token in tokens:
            self._validate_candidate_path(
                token,
                project_root=project_root,
                attempted_action=command,
                user_id=user_id,
            )

    def _validate_candidate_path(
        self,
        candidate: str,
        *,
        project_root: Path,
        attempted_action: str,
        user_id: int | None,
    ) -> None:
        resolved = self._resolve_candidate_path(candidate, project_root)
        if not self._is_within(resolved, project_root):
            self._raise_violation(
                "Security Error: Permission Denied. Stay in project folder.",
                attempted_action=attempted_action,
                category="path_traversal",
                user_id=user_id,
            )

        if self._is_sensitive_path(resolved):
            self._raise_violation(
                "Security Error: Sensitive file access blocked.",
                attempted_action=attempted_action,
                category="sensitive_path",
                user_id=user_id,
            )

    def _resolve_candidate_path(self, candidate: str, project_root: Path) -> Path:
        candidate_path = Path(candidate).expanduser()
        if not candidate_path.is_absolute():
            candidate_path = project_root / candidate_path
        return candidate_path.resolve(strict=False)

    def _extract_path_tokens(self, command: str) -> list[str]:
        tokens: list[str] = []
        for token in shlex.split(command, posix=True):
            if token.startswith("-"):
                continue
            if token.startswith(("~", "/", ".")) or "/" in token:
                tokens.append(token)
        return tokens

    def _is_sensitive_path(self, candidate: Path) -> bool:
        candidate_str = candidate.as_posix()
        return any(pattern.search(candidate_str) for pattern in _COMMAND_PATTERNS[3:])

    def _is_within(self, candidate: Path, root: Path) -> bool:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            return False

    def _raise_violation(
        self,
        message: str,
        *,
        attempted_action: str,
        category: str,
        user_id: int | None,
    ) -> None:
        self._write_audit_log(
            category=category,
            user_id=user_id,
            attempted_action=attempted_action,
        )
        raise SecurityViolation(message, attempted_action, category=category)

    def _write_audit_log(
        self,
        *,
        category: str,
        user_id: int | None,
        attempted_action: str,
    ) -> None:
        # Аудит — best-effort: невозможность записать лог (нет прав на каталог,
        # read-only ФС) НЕ должна мешать блокировке. Иначе PermissionError из
        # mkdir/open вылетает до `raise SecurityViolation`, и в SDK-firewall-хуке
        # его ловит fail-open `except Exception → {}` → инструмент пропускается.
        try:
            self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "category": category,
                "user_id": user_id,
                "attempted_action": attempted_action,
            }
            with self.audit_log_path.open("a", encoding="utf-8") as audit_log:
                audit_log.write(json.dumps(record, ensure_ascii=True))
                audit_log.write("\n")
        except OSError as exc:
            logger.warning("security_audit_log_write_failed", error=str(exc))
