"""Helpers for managing Claude MCP server configuration."""
from __future__ import annotations

import json
import re
import secrets
import shlex
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse


_NPM_URL_RE = re.compile(r"https?://(?:www\.)?npmjs\.com/package/(?P<package>[^?\s]+)")
_GITHUB_URL_RE = re.compile(r"https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s?#]+)(?P<tail>/[^\s?#]*)?")


@dataclass(frozen=True)
class McpCatalogTool:
    tool_id: str
    title: str
    install_spec: str
    description: str


@dataclass(frozen=True)
class McpCandidate:
    server_name: str
    install_spec: str
    source_url: str


@dataclass(frozen=True)
class McpOperationResult:
    message: str
    server_name: str
    created: bool = False
    removed: bool = False


class McpManager:
    """Manage MCP server registrations in Claude config."""

    CATALOG: dict[str, McpCatalogTool] = {
        "browser": McpCatalogTool(
            tool_id="browser",
            title="Browser",
            install_spec="@modelcontextprotocol/server-puppeteer",
            description="Browser automation via Puppeteer",
        ),
        "postgres": McpCatalogTool(
            tool_id="postgres",
            title="PostgreSQL",
            install_spec="@modelcontextprotocol/server-postgres",
            description="PostgreSQL database access",
        ),
        "github": McpCatalogTool(
            tool_id="github",
            title="GitHub",
            install_spec="@modelcontextprotocol/server-github",
            description="GitHub repository access",
        ),
    }

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = self._resolve_config_path(config_path)
        self._pending_candidates: dict[str, McpCandidate] = {}

    def describe_installed(self) -> str:
        """Describe currently installed MCP servers (primary view)."""
        config = self._read_config()
        servers = config.get("mcpServers", {})
        lines = ["🔌 <b>MCP-серверы</b>"]

        if servers:
            lines.append("")
            lines.append(f"<b>Установлено ({len(servers)}):</b>")
            for name, server_config in servers.items():
                cmd = server_config.get("command", "")
                args = " ".join(server_config.get("args", []))
                source = server_config.get("source", "")
                display = source or f"{cmd} {args}".strip()
                lines.append(f"  • <code>{name}</code> — {display}")
        else:
            lines.append("")
            lines.append("<i>Нет установленных серверов.</i>")

        # Show catalog suggestions
        installed = self.installed_server_names()
        not_installed = [t for t in self.CATALOG.values() if t.tool_id not in installed]
        if not_installed:
            lines.append("")
            lines.append("<b>Доступно для установки:</b>")
            for tool in not_installed:
                lines.append(f"  • <b>{tool.title}</b> — {tool.description}")

        lines.append("")
        lines.append("Команды: <code>/mcp install &lt;npm|npx|github|path&gt;</code>, <code>/mcp remove &lt;name&gt;</code>")
        return "\n".join(lines)

    def describe_catalog(self) -> str:
        """Alias kept for backward compatibility — now delegates to describe_installed."""
        return self.describe_installed()

    def installed_server_names(self) -> set[str]:
        config = self._read_config()
        return set(config.get("mcpServers", {}).keys())

    def install_catalog_tool(self, tool_id: str) -> McpOperationResult:
        tool = self.CATALOG.get(tool_id)
        if tool is None:
            raise ValueError(f"Unknown MCP catalog tool: {tool_id}")
        return self.install(tool.install_spec, preferred_name=tool.tool_id)

    def install_pending(self, token: str) -> McpOperationResult:
        candidate = self._pending_candidates.pop(token, None)
        if candidate is None:
            return McpOperationResult(
                message="Ссылка для установки устарела. Отправьте её снова.",
                server_name="unknown",
            )
        return self.install(candidate.install_spec, preferred_name=candidate.server_name)

    def register_candidate(self, candidate: McpCandidate) -> str:
        token = secrets.token_urlsafe(6)
        self._pending_candidates[token] = candidate
        return token

    def remove(self, server_name: str) -> McpOperationResult:
        normalized_name = _normalize_server_name(server_name)
        config = self._read_config()
        servers = config.setdefault("mcpServers", {})
        if normalized_name not in servers:
            return McpOperationResult(
                message=f"⚠️ MCP <code>{normalized_name}</code> не найден.",
                server_name=normalized_name,
            )

        del servers[normalized_name]
        self._write_config(config)
        return McpOperationResult(
            message=f"🗑 Удалён MCP <code>{normalized_name}</code>.",
            server_name=normalized_name,
            removed=True,
        )

    def install(self, spec: str, preferred_name: str | None = None) -> McpOperationResult:
        parsed = _parse_install_spec(spec, preferred_name=preferred_name)
        config = self._read_config()
        servers = config.setdefault("mcpServers", {})
        created = parsed["name"] not in servers
        servers[parsed["name"]] = {
            "command": parsed["command"],
            "args": parsed["args"],
            "source": parsed["source"],
        }
        self._write_config(config)

        status = "Добавлен" if created else "Обновлён"
        return McpOperationResult(
            message=(
                f"✅ {status} MCP <code>{parsed['name']}</code>.\n"
                f"Команда: <code>{parsed['command']} {' '.join(parsed['args'])}</code>\n"
                "Инструмент будет доступен в следующем запросе к Claude."
            ),
            server_name=parsed["name"],
            created=created,
        )

    def _read_config(self) -> dict:
        try:
            raw = self.config_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                data.setdefault("mcpServers", {})
                return data
        except FileNotFoundError:
            pass
        return {"mcpServers": {}}

    def _write_config(self, data: dict) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _resolve_config_path(config_path: Path | None) -> Path:
        if config_path is not None:
            return config_path

        # Claude Code stores MCP servers in ~/.claude.json (main config)
        # Fallback to dedicated mcp.json files if they exist
        candidates = [
            Path.home() / ".claude.json",
            Path.home() / ".claude" / "mcp.json",
            Path.home() / ".config" / "claude" / "mcp.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]


def detect_mcp_candidate(text: str) -> McpCandidate | None:
    """Extract a likely MCP install target from a Telegram message."""
    lower_text = text.lower()
    looks_like_mcp = "mcp" in lower_text or "modelcontextprotocol" in lower_text
    if not looks_like_mcp:
        return None

    npm_match = _NPM_URL_RE.search(text)
    if npm_match:
        package = npm_match.group("package")
        return McpCandidate(
            server_name=_normalize_server_name(package),
            install_spec=package,
            source_url=npm_match.group(0),
        )

    github_match = _GITHUB_URL_RE.search(text)
    if github_match:
        owner = github_match.group("owner")
        repo = github_match.group("repo").removesuffix(".git")
        tail = github_match.group("tail") or ""
        server_name = repo
        tail_parts = [part for part in tail.split("/") if part]
        if "src" in tail_parts:
            src_index = tail_parts.index("src")
            if src_index + 1 < len(tail_parts):
                server_name = tail_parts[src_index + 1]
        install_spec = _github_install_spec(owner=owner, repo=repo, server_name=server_name)
        return McpCandidate(
            server_name=_normalize_server_name(server_name),
            install_spec=install_spec,
            source_url=github_match.group(0),
        )

    return None


@lru_cache(maxsize=1)
def get_mcp_manager() -> McpManager:
    """Return the process-wide MCP manager used by Telegram handlers."""
    return McpManager()


def _parse_install_spec(spec: str, preferred_name: str | None = None) -> dict[str, object]:
    raw_spec = spec.strip()
    if not raw_spec:
        raise ValueError("empty MCP install spec")

    npm_url_match = _NPM_URL_RE.fullmatch(raw_spec)
    if npm_url_match:
        raw_spec = npm_url_match.group("package")

    github_url_match = _GITHUB_URL_RE.fullmatch(raw_spec)
    if github_url_match:
        owner = github_url_match.group("owner")
        repo = github_url_match.group("repo").removesuffix(".git")
        tail = github_url_match.group("tail") or ""
        server_name = repo
        tail_parts = [part for part in tail.split("/") if part]
        if "src" in tail_parts:
            src_index = tail_parts.index("src")
            if src_index + 1 < len(tail_parts):
                server_name = tail_parts[src_index + 1]
        raw_spec = _github_install_spec(owner=owner, repo=repo, server_name=server_name)

    tokens = shlex.split(raw_spec)
    if not tokens:
        raise ValueError("empty MCP install spec")

    first = tokens[0]
    if first == "npx":
        package = _first_non_flag(tokens[1:])
        if package is None:
            raise ValueError("npx command must include a package")
        return {
            "name": _normalize_server_name(preferred_name or package),
            "command": "npx",
            "args": tokens[1:],
            "source": raw_spec,
        }

    parsed_url = urlparse(first)
    if parsed_url.scheme == "github":
        repo = parsed_url.path.strip("/")
        server_name = preferred_name or repo.rsplit("/", 1)[-1]
        return {
            "name": _normalize_server_name(server_name),
            "command": "npx",
            "args": ["-y", raw_spec],
            "source": raw_spec,
        }

    if first.startswith(("/", "./", "../", "~")):
        expanded = str(Path(first).expanduser())
        return {
            "name": _normalize_server_name(preferred_name or Path(first).stem),
            "command": expanded,
            "args": tokens[1:],
            "source": raw_spec,
        }

    return {
        "name": _normalize_server_name(preferred_name or first),
        "command": "npx",
        "args": ["-y", *tokens],
        "source": raw_spec,
    }


def _first_non_flag(tokens: list[str]) -> str | None:
    for token in tokens:
        if not token.startswith("-"):
            return token
    return None


def _normalize_server_name(value: str) -> str:
    tail = value.strip().rstrip("/").rsplit("/", 1)[-1]
    return tail.removeprefix("@").replace("/", "-")


def _github_install_spec(*, owner: str, repo: str, server_name: str) -> str:
    if owner == "modelcontextprotocol" and repo == "servers":
        return f"@modelcontextprotocol/server-{_normalize_server_name(server_name)}"
    return f"github:{owner}/{repo}"
