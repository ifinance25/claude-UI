"""Curated catalog of token-based services users can connect (Phase 1).

Each service maps a user-pasted secret to an MCP server definition consumable
by claude-agent-sdk (``mcp_servers``) / CLI ``--mcp-config``. Adding a service
is one ``ServiceDef`` entry. OAuth services are a future phase.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ServiceDef:
    id: str
    name: str
    icon: str
    description: str
    how_to_url: str
    how_to_steps: tuple[str, ...]
    secret_label: str
    build_mcp_server: Callable[[str], dict[str, Any]]


def _notion(secret: str) -> dict[str, Any]:
    return {
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        "env": {"NOTION_TOKEN": secret},
    }


def _github(secret: str) -> dict[str, Any]:
    # Официальный УДАЛЁННЫЙ GitHub MCP (hosted): аутентификация PAT через заголовок
    # Authorization: Bearer — локальный пакет/бинарь не нужен. Старый npx-пакет
    # @modelcontextprotocol/server-github устарел/архивирован.
    return {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": f"Bearer {secret}"},
    }


CATALOG: tuple[ServiceDef, ...] = (
    ServiceDef(
        id="notion",
        name="Notion",
        icon="📝",
        description="Читать и обновлять страницы и базы Notion.",
        how_to_url="https://www.notion.so/my-integrations",
        how_to_steps=(
            "Открой notion.so/my-integrations и создай Internal integration.",
            "Скопируй Internal Integration Token.",
            "На нужных страницах: ••• → Connections → добавь интеграцию.",
        ),
        secret_label="Integration token",
        build_mcp_server=_notion,
    ),
    ServiceDef(
        id="github",
        name="GitHub",
        icon="🐙",
        description="Доступ к репозиториям, issues и PR на GitHub.",
        how_to_url="https://github.com/settings/tokens",
        how_to_steps=(
            "Открой github.com/settings/tokens (fine-grained или classic).",
            "Создай токен с доступом к нужным репозиториям.",
            "Скопируй Personal Access Token (показывается один раз).",
        ),
        secret_label="Personal access token",
        build_mcp_server=_github,
    ),
)

_BY_ID = {s.id: s for s in CATALOG}


def get_service(service_id: str) -> ServiceDef | None:
    return _BY_ID.get(service_id)
