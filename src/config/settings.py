"""Application settings and configuration."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger()


def _parse_bool(value: str, *, default: bool) -> bool:
    """Разбор строкового флага (env) в bool, регистронезависимо.

    ``1/true/yes/on`` → True, ``0/false/no/off`` → False. Пустая или
    нераспознанная строка → ``default``. Используется для env-оверрайдов
    вроде ``CLAUDE_REQUIRE_USER_KEY``.
    """
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивный merge ``override`` поверх ``base``.

    Словари сливаются ключ-по-ключу, остальные значения (включая
    списки) — переписываются. Списки не конкатенируются, чтобы локальный
    override мог полностью переопределить, например, ``projects.paths``.
    """
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


class TelegramSettings(BaseModel):
    """Telegram bot settings."""

    token: str = Field(default="", description="Bot token from @BotFather")
    chat_id: int | None = Field(
        default=None,
        description="Default Telegram chat ID for automated jobs",
    )


class SecuritySettings(BaseModel):
    """Security settings."""

    allowed_user_ids: list[int] = Field(
        default_factory=list,
        description="Telegram user IDs allowed to use the bot",
    )


class ProjectsSettings(BaseModel):
    """Projects configuration."""

    scan_directory: str | None = Field(
        default="~/projects",
        description="Directory to scan for projects",
    )
    paths: list[str] | None = Field(
        default=None,
        description="Explicit list of project paths",
    )


class ClaudeSettings(BaseModel):
    """Claude Code settings."""

    transport: Literal["sdk", "cli", "tmux"] = Field(
        default="sdk",
        description="Transport mode: sdk (Claude Agent SDK), cli (subprocess), tmux",
    )
    permission_mode: Literal["bypassPermissions", "acceptEdits", "plan"] = Field(
        default="bypassPermissions",
        description="Permission mode for Claude Code",
    )
    timeout_minutes: int = Field(
        default=30,
        description="Timeout for operations in minutes",
    )
    max_turns: int = Field(
        default=100,
        description="Maximum turns per request",
    )
    idle_timeout_seconds: int = Field(
        default=60,
        description="Maximum silence before smart monitor stops Claude",
    )
    require_jail: bool = Field(
        default=False,
        description=(
            "Fail-CLOSED политика OS-джейла для confine'нутых сессий: если True, "
            "а bwrap/unprivileged-userns недоступны — сессия с ограничением "
            "доступа ОТКАЗЫВАЕТСЯ стартовать (не запускается без изоляции). "
            "False (по умолчанию) — best-effort для соло self-host."
        ),
    )
    require_user_key: bool = Field(
        default=True,
        description="Require per-user Anthropic API keys for non-privileged users (SP2)",
    )


class DisplaySettings(BaseModel):
    """Display and UI settings."""

    show_token_usage: bool = Field(
        default=True,
        description="Show token usage after responses",
    )
    show_context_usage: bool = Field(
        default=False,
        description="Show context window usage (tokens/percentage) in status",
    )
    show_logs: bool = Field(
        default=True,
        description="Show Claude Code logs during processing",
    )
    log_update_interval_ms: int = Field(
        default=1000,
        description="Log message update interval in milliseconds",
    )
    keep_log_after_response: bool = Field(
        default=False,
        description="Keep the progress log message visible after the final response is sent",
    )


class LimitsSettings(BaseModel):
    """Limits and thresholds."""

    max_message_length: int = Field(
        default=4096,
        description="Maximum Telegram message length",
    )
    code_as_file_threshold: int = Field(
        default=500,
        description="Code blocks larger than this are sent as files",
    )


class WebhookSettings(BaseModel):
    """Webhook API server settings."""

    enabled: bool = Field(
        default=False,
        description=(
            "Enable the embedded webhook server. OFF by default — это приёмник "
            "внешних HTTP-событий, который запускает Claude с bypassPermissions. "
            "Включать только вместе с непустым secret и bind на 127.0.0.1 за "
            "reverse-proxy. Заполнение telegram.chat_id+webhooks.topic_id при "
            "пустом secret = публичный RCE."
        ),
    )
    host: str = Field(
        default="127.0.0.1",
        description=(
            "Host interface for the webhook server. 127.0.0.1 (loopback) по "
            "умолчанию — наружу проксировать только через Caddy/nginx с секретом. "
            "Не биндить на 0.0.0.0."
        ),
    )
    port: int = Field(
        default=8080,
        description="Port for the webhook server",
    )
    secret: str = Field(
        default="",
        description="Shared secret used for webhook authentication",
    )
    topic_id: int = Field(
        default=0,
        description="Telegram forum topic ID for webhook-triggered Claude sessions",
    )
    default_project_path: str | None = Field(
        default=None,
        description="Default project path for webhook-triggered Claude sessions",
    )


class CronJobSettings(BaseModel):
    """Scheduled Claude Code job configuration."""

    name: str = Field(default="", description="Human-readable job name")
    schedule: str = Field(
        default="",
        description="Cron expression or interval string such as '0 9 * * *' or 'every 15m'",
    )
    prompt: str = Field(default="", description="Prompt to send to Claude when the job runs")
    topic_id: int = Field(default=0, description="Telegram forum topic to post results into")
    chat_id: int | None = Field(
        default=None,
        description="Optional Telegram chat ID override for this job",
    )
    project_path: str | None = Field(
        default=None,
        description="Optional project path override for this job",
    )
    project_name: str | None = Field(
        default=None,
        description="Optional project name override for this job",
    )
    enabled: bool = Field(default=True, description="Whether this job is active")


class PersistenceSettings(BaseModel):
    """Persistence settings."""

    session_database_path: str = Field(
        default="data/sessions.db",
        description="SQLite database path for persisted topic sessions",
    )


class WebSettings(BaseModel):
    """Web UI server settings."""

    enabled: bool = Field(
        default=False,
        description="Enable the embedded web UI server",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Host interface for the web UI server (127.0.0.1 for SSH-tunnel-only v1)",
    )
    port: int = Field(
        default=8765,
        description="Port for the web UI server",
    )
    jwt_secret: str | None = Field(
        default=None,
        description=(
            "HMAC secret for issuing JWT cookies. Generated by installer "
            "if not provided. Required when web.enabled=true — обязателен,"
            " пустая/None строка не принимается."
        ),
    )
    jwt_ttl_days: int = Field(
        default=7,
        description=(
            "JWT cookie lifetime in days. Снижено с 30 до 7: stateless-JWT нельзя "
            "отозвать до истечения, поэтому короткий TTL ограничивает окно "
            "переиспользования украденной/устаревшей куки."
        ),
    )
    telegram_bot_username: str = Field(
        default="",
        description="Bot username (without @) for Telegram Login Widget",
    )
    public_origin: str = Field(
        default="",
        description="Public origin (e.g. https://vels.example.com) used to build magic-link URLs.",
    )
    cookie_secure: bool | None = Field(
        default=None,
        description=(
            "Force Secure attribute on session cookies. None (default) "
            "auto-detects from public_origin's scheme; True/False overrides. "
            "Set True in production behind HTTPS even if public_origin "
            "starts with http:// (e.g. nginx terminating TLS upstream)."
        ),
    )
    dev_bearer_token: str = Field(
        default="",
        description="Static bearer token for v1 dev auth without HTTPS. Empty disables dev-login.",
    )
    dev_login_enabled: bool = Field(
        default=True,
        description=(
            "Включён ли путь /api/auth/dev-login (статический bearer). H-6: "
            "статический долгоживущий секрет — слабое место. В проде после "
            "перехода на magic-link установите false, чтобы полностью закрыть "
            "этот путь входа. False → /api/auth/dev-login отвечает 404."
        ),
    )
    scratch_dir: str | None = Field(
        default=None,
        description=(
            "Рабочий каталог для сессий БЕЗ проекта («чистый Claude»). "
            "None → <data_dir>/scratch (рядом с sessions.db). Должен лежать "
            "ВНЕ любого настроенного project root — это отдельная песочница."
        ),
    )


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Environment overrides
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    allowed_user_ids_env: str = Field(default="", alias="ALLOWED_USER_IDS")
    projects_dir_env: str = Field(default="", alias="PROJECTS_DIR")
    session_database_path_env: str = Field(default="", alias="SESSION_DATABASE_PATH")
    web_jwt_secret_env: str = Field(default="", alias="WEB_JWT_SECRET")
    web_dev_bearer_token_env: str = Field(default="", alias="WEB_DEV_BEARER_TOKEN")
    connections_secret_key_env: str = Field(default="", alias="CONNECTIONS_SECRET_KEY")
    telegram_bot_username_env: str = Field(default="", alias="TELEGRAM_BOT_USERNAME")
    admin_login_env: str = Field(default="", alias="ADMIN_LOGIN")
    admin_password_env: str = Field(default="", alias="ADMIN_PASSWORD")
    require_user_key_env: str = Field(default="", alias="CLAUDE_REQUIRE_USER_KEY")
    owner_user_id_env: str = Field(default="", alias="OWNER_USER_ID")

    # Nested settings (from config file)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    projects: ProjectsSettings = Field(default_factory=ProjectsSettings)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    webhooks: WebhookSettings = Field(default_factory=WebhookSettings)
    persistence: PersistenceSettings = Field(default_factory=PersistenceSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    cron_scheduler_enabled: bool = Field(
        default=False,
        description="Enable built-in cron scheduler",
    )
    cron_jobs: list[CronJobSettings] = Field(
        default_factory=list,
        description="Configured built-in scheduler jobs",
    )

    @classmethod
    def from_yaml(cls, config_path: str | Path) -> "Settings":
        """Load settings from YAML file with environment variable expansion.

        Если рядом с ``config_path`` лежит ``<stem>.local<suffix>``
        (например, ``config/config.local.yaml`` для
        ``config/config.yaml``), его значения накладываются ПОВЕРХ
        базового конфига — это позволяет держать локальные dev-настройки
        (``web.enabled: true``, ``public_origin``, ``projects.paths``)
        вне git, не трогая трекаемый файл. ``*.local.yaml`` уже
        в ``.gitignore`` (паттерн ``*.local``).
        """
        config_path = Path(config_path)

        if not config_path.exists():
            return cls()

        # Явный utf-8: в конфиге кириллические комментарии — без этого на
        # Windows (cp1252) чтение падает с UnicodeDecodeError.
        with open(config_path, encoding="utf-8") as f:
            raw_config = f.read()

        # Expand environment variables in the format ${VAR_NAME}
        expanded_config = os.path.expandvars(raw_config)
        config_data = yaml.safe_load(expanded_config) or {}

        # Merge local override if present.
        local_path = config_path.with_name(
            f"{config_path.stem}.local{config_path.suffix}"
        )
        if local_path.exists():
            with open(local_path, encoding="utf-8") as f:
                local_raw = f.read()
            local_expanded = os.path.expandvars(local_raw)
            local_data = yaml.safe_load(local_expanded) or {}
            config_data = _deep_merge_dicts(config_data, local_data)

        return cls(**config_data)

    def get_bot_token(self) -> str:
        """Get the bot token with priority: env > config."""
        return self.telegram_bot_token or self.telegram.token

    def get_default_chat_id(self) -> int | None:
        """Get the default Telegram chat ID for automation."""
        return self.telegram.chat_id

    def get_allowed_user_ids(self) -> list[int]:
        """Get allowed user IDs with priority: env > config."""
        if self.allowed_user_ids_env:
            return [int(uid.strip()) for uid in self.allowed_user_ids_env.split(",")]
        return self.security.allowed_user_ids

    def get_owner_user_id(self) -> int | None:
        """ID владельца для промоута в админы на свежей БД (L-2).

        Приоритет: явный ``OWNER_USER_ID`` из .env (установщик пишет туда id,
        который оператор указал как СВОЙ) > первый из ``ALLOWED_USER_IDS``.
        Явный источник исключает ситуацию, когда учитель вписал ученика раньше
        себя и админом молча стал ученик. ``None`` — если ничего не настроено.
        """
        raw = (self.owner_user_id_env or "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                logger.warning("owner_user_id_env_invalid", value=raw)
        allowed = self.get_allowed_user_ids()
        return allowed[0] if allowed else None

    def get_projects_directory(self) -> Path:
        """Get projects directory with priority: env > config."""
        dir_str = self.projects_dir_env or self.projects.scan_directory or "~/projects"
        return Path(dir_str).expanduser()

    def get_project_paths(self) -> list[Path]:
        """Get all project paths (from explicit list or by scanning directory)."""
        # If explicit paths are provided, use them
        if self.projects.paths:
            return [Path(p).expanduser() for p in self.projects.paths]

        # Otherwise, scan the directory
        projects_dir = self.get_projects_directory()
        if not projects_dir.exists():
            return []

        # Get all non-hidden subdirectories
        project_paths = [
            item
            for item in projects_dir.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        ]

        return sorted(project_paths, key=lambda p: p.name.lower())

    def get_session_database_path(self) -> Path:
        """Get the SQLite database path for persisted topic sessions."""
        path_str = self.session_database_path_env or self.persistence.session_database_path
        return Path(path_str).expanduser()

    def get_connections_secret_key(self) -> str:
        """Fernet master key for per-user connection secrets (env only)."""
        return self.connections_secret_key_env or ""

    def get_scratch_dir(self) -> Path:
        """Каталог для сессий без проекта («чистый Claude»).

        Приоритет: web.scratch_dir > ``<data_dir>/scratch``, где data_dir —
        родитель БД сессий. Каталог НЕ обязан существовать заранее —
        создаётся лениво в момент первого использования (routes_ws). Это
        отдельная песочница вне любого project root.
        """
        if self.web.scratch_dir:
            return Path(self.web.scratch_dir).expanduser()
        return self.get_session_database_path().parent / "scratch"

    def get_web_jwt_secret(self) -> str:
        """Get the JWT secret for web cookies (env > config)."""
        return self.web_jwt_secret_env or (self.web.jwt_secret or "")

    def get_web_dev_bearer_token(self) -> str:
        """Get the v1 dev bearer token (env > config). Empty disables dev-login."""
        return self.web_dev_bearer_token_env or self.web.dev_bearer_token

    def get_telegram_bot_username(self) -> str:
        """Get the bot username for Telegram Login Widget (env > config)."""
        return self.telegram_bot_username_env or self.web.telegram_bot_username

    def get_admin_login(self) -> str:
        """Логин первого админа из .env (ADMIN_LOGIN). Пусто — bootstrap пропускается."""
        return self.admin_login_env

    def get_admin_password(self) -> str:
        """Пароль первого админа из .env (ADMIN_PASSWORD)."""
        return self.admin_password_env

    @property
    def require_user_key(self) -> bool:
        """Require per-user Anthropic API keys for non-privileged users (SP2).

        Fail-closed: по умолчанию True. Приоритет: env
        ``CLAUDE_REQUIRE_USER_KEY`` (регистронезависимо) >
        ``claude.require_user_key`` из конфига. Читается из relay/бота
        как ``getattr(settings, "require_user_key", True)``.
        """
        if self.require_user_key_env.strip():
            return _parse_bool(self.require_user_key_env, default=True)
        return self.claude.require_user_key


@lru_cache
def get_settings(config_path: str = "config/config.yaml") -> Settings:
    """Get cached settings instance."""
    return Settings.from_yaml(config_path)
