"""Typed bus events."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BusEvent(BaseModel):
    """Base class for all bus events."""

    model_config = ConfigDict(arbitrary_types_allowed=True)


class UserMessageReceived(BusEvent):
    """Telegram or another producer requests an agent run."""

    request_id: str
    chat_id: int
    # Telegram user id of the sender. Used to resolve per-user MCP servers
    # for the relay dispatch. Default 0 keeps back-compat with producers that
    # don't set it (web/webhook/cron), which resolve to no connections.
    user_id: int = 0
    topic_id: int
    project_path: str
    project_name: str = ""
    text: str
    session_id: str | None = None
    attachments: list[Any] = Field(default_factory=list)
    # Public cross-channel identifier of the session. Populated by all
    # publishers (web WS reader, Telegram handler). Default empty
    # preserves backward compatibility with callers that haven't
    # migrated yet.
    session_uuid: str = ""
    # Which channel originated the message. Lets web clients render
    # TG-originated messages with a distinct marker, and lets analytics
    # split usage by entry point. Default "telegram" matches the
    # historical assumption (only the TG bot existed pre-web).
    source: Literal["telegram", "web", "webhook", "cron"] = "telegram"
    # Доступ «только чтение»: Claude запускается без права менять файлы
    # (Write/Edit/Bash запрещены). Выставляется веб-публикатором по уровню
    # доступа пользователя к проекту. По умолчанию полный доступ.
    readonly: bool = False
    # Привилегированный отправитель (admin/whitelist). Выставляется веб-
    # публикатором той же логикой, что решает confine_root (privileged →
    # без confine). Downstream-политика (напр. выбор джейла/креденшелов)
    # использует это поле. По умолчанию непривилегированный.
    privileged: bool = False
    # H-1: корень для PreToolUse-firewall. Для непривилегированного (не admin/
    # не whitelist) юзера с FULL-доступом к проекту confine'им Claude в корень
    # проекта (запрет выхода/секретов/деструктива). None → доверенная сессия
    # (admin/whitelist/no-project), firewall не навешивается.
    confine_root: str | None = None


class AgentStarted(BusEvent):
    """Claude processing has started for a request."""

    request_id: str
    chat_id: int
    topic_id: int
    session_uuid: str = ""


class AgentStreamingUpdate(BusEvent):
    """A streamed Claude update intended for subscribers."""

    request_id: str
    chat_id: int
    topic_id: int
    kind: Literal[
        "init",
        "log",
        "tool_use",
        "subagent_start",
        "subagent_log",
        "subagent_finish",
        "text",
        "thinking",
        "usage",
    ]
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    session_uuid: str = ""


class AgentFinished(BusEvent):
    """A Claude request completed or failed."""

    request_id: str
    chat_id: int
    topic_id: int
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    response_text: str = ""
    error: str | None = None
    session_uuid: str = ""


class WebhookTriggered(BusEvent):
    """Normalized webhook received from the embedded API server."""

    source: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    delivery_id: str | None = None
    repository: str | None = None
    action: str | None = None
    ref: str | None = None


class CostLimitExceeded(BusEvent):
    """Billing guardrail exceeded for a topic/request."""

    topic_id: int
    reason: str
    cost_usd: float | None = None
    limit_usd: float | None = None
