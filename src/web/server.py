"""FastAPI app for the web UI + uvicorn lifecycle.

Runs inside the same asyncio loop as the aiogram bot. Started/stopped from
``TelegramClaudeBot.start()`` / ``stop()`` analogously to ``WebhookAPIServer``
and ``CronScheduler``.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.config.settings import WebSettings
from src.web.magic_link import MagicLinkStore
from src.web.origin_check import build_allowed_origins, is_allowed_origin
from src.web.routes_admin import make_admin_router
from src.web.routes_apikey import make_apikey_router
from src.web.routes_auth import make_auth_router
from src.web.routes_connections import make_connections_router
from src.web.routes_docs import make_docs_router
from src.web.routes_files import make_files_router
from src.web.routes_members import make_members_router
from src.web.routes_model import make_model_router
from src.web.routes_projects import make_projects_router
from src.web.routes_sessions import make_sessions_router
from src.web.routes_settings import make_settings_router
from src.web.routes_uploads import make_uploads_router
from src.web.routes_ws import make_ws_router
from src.web.running_sessions import RunningSessionsTracker
from src.web.ws_forwarder import WSForwarder

logger = structlog.get_logger()


def mount_frontend(app: FastAPI, dist_dir) -> None:
    """Отдаёт собранный фронт (web/dist) с SPA-fallback на index.html.
    No-op, если каталога нет (dev-режим с отдельным vite). Монтировать
    ПОСЛЕ всех /api-роутеров, иначе catch-all перехватит API."""
    from pathlib import Path

    from src.utils.url_safety import is_path_within_root

    dist_dir = Path(dist_dir).resolve()
    index = dist_dir / "index.html"
    if not index.is_file():
        return
    assets = dist_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> FileResponse:
        # full_path недоверенный. Конвертер :path НЕ схлопывает
        # URL-кодированные `..` (%2e%2e%2f), поэтому без проверки границ
        # FileResponse отдал бы любой файл с диска (unauth traversal,
        # утечка .env/WEB_JWT_SECRET → подделка admin-JWT). Резолвим и
        # проверяем, что кандидат внутри dist_dir.
        candidate = (dist_dir / full_path).resolve()
        if (
            full_path
            and candidate.is_file()
            and is_path_within_root(dist_dir, candidate)
        ):
            return FileResponse(candidate)
        return FileResponse(index)


class WebServer:
    """FastAPI + uvicorn server living inside the bot's asyncio loop."""

    def __init__(
        self,
        *,
        settings: WebSettings,
        allowed_user_ids: list[int],
        bot_username: str,
        session_manager: Any,
        event_bus: Any,
        bot_token: str = "",
        jwt_secret: str = "",
        dev_bearer_token: str = "",
        project_paths: list | None = None,
        magic_link_store: MagicLinkStore | None = None,
        admin_login: str = "",
        admin_password: str = "",
        scratch_dir: str | Path | None = None,
        connections_store: Any = None,
        api_key_store: Any = None,
        projects_dir: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.allowed_user_ids = allowed_user_ids
        self.bot_username = bot_username
        self.session_manager = session_manager
        self.event_bus = event_bus
        self.bot_token = bot_token
        self.jwt_secret = jwt_secret or (settings.jwt_secret or "")
        self.dev_bearer_token = dev_bearer_token or settings.dev_bearer_token
        self.project_paths = project_paths or []
        # Рабочий каталог для сессий без проекта («чистый Claude»). None →
        # сессии без проекта стартуют в cwd процесса (legacy/тесты без него).
        self.scratch_dir = scratch_dir
        self.magic_link_store = magic_link_store or MagicLinkStore()
        # Креды первого админа (из .env через Settings; pydantic грузит .env
        # в объект настроек, а НЕ в os.environ — поэтому читаем отсюда).
        self.admin_login = admin_login
        self.admin_password = admin_password
        # Хранилище per-user подключений к сервисам (MCP). None → фича
        # выключена (CONNECTIONS_SECRET_KEY не задан) — /api/connections тогда
        # отвечает enabled:false / 503 на мутациях.
        self.connections_store = connections_store
        # Хранилище per-user Anthropic API-ключей (SP2). None → фича выключена
        # (CONNECTIONS_SECRET_KEY не задан) — /api/apikey тогда отвечает 501.
        self.api_key_store = api_key_store
        self.projects_dir = Path(
            projects_dir or "/var/lib/vels-bot/projects"
        )

        self._uvicorn_server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task | None = None
        self._ws_forwarder: WSForwarder | None = (
            WSForwarder(bus=event_bus) if event_bus is not None else None
        )
        # Отслеживает session_uuid с активной генерацией (AgentStarted/Finished),
        # чтобы листинг сессий выдавал is_running для индикатора «думает».
        self._running_tracker: RunningSessionsTracker | None = (
            RunningSessionsTracker(event_bus) if event_bus is not None else None
        )

        # Lifespan starts the WSForwarder before the first WS handshake. Tests
        # use TestClient which drives the lifespan automatically.
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            if self._ws_forwarder is not None:
                await self._ws_forwarder.start()
            try:
                yield
            finally:
                if self._ws_forwarder is not None:
                    await self._ws_forwarder.stop()

        self.app = FastAPI(
            title="Vels-Claude Web UI",
            openapi_url="/api/openapi.json",
            docs_url=None,
            redoc_url=None,
            lifespan=lifespan,
        )

        self._register_routes()

    def _effective_project_paths(self) -> list:
        """Корни проектов = config-пути ∪ проекты из БД (админ-таб «Проекты»).

        Единый источник для всех роутеров: благодаря этому проект, добавленный
        админом через UI (любым абсолютным путём), сразу становится видимым в
        выборе сессии и выдаваемым через resolve_project_access — без правки
        конфигов. ``session_manager is None`` (тесты/legacy) → только config.
        Ошибка БД не валит запрос: возвращаем хотя бы config-пути.
        """
        paths: list = list(self.project_paths)
        if self.session_manager is not None:
            try:
                for row in self.session_manager.list_all_projects():
                    abspath = row.get("abspath")
                    if abspath:
                        paths.append(Path(abspath))
            except Exception as exc:  # noqa: BLE001 — БД не должна ронять выдачу
                logger.warning("effective_project_paths_db_failed", error=str(exc))
        seen: set[str] = set()
        unique: list = []
        for p in paths:
            # Дедуп по resolve(): схлопывает ~/x, /home/u/x/, симлинк-варианты
            # одного и того же каталога, чтобы /api/projects не дублировал проект.
            try:
                key = str(Path(p).expanduser().resolve())
            except (OSError, ValueError):
                key = str(p)
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    def _register_routes(self) -> None:
        # Разрешённые origin'ы (для CSRF/CSWSH) — из public_origin. Same-origin
        # и loopback допускаются автоматически (см. origin_check).
        allowed_origins = build_allowed_origins(self.settings.public_origin)
        # L-6: loopback-Origin разрешаем только в dev. В проде (public_origin —
        # реальный не-loopback домен) localhost/127.0.0.1 как Origin не нужны.
        from src.utils.url_safety import is_loopback_url as _is_loopback_url

        _po = self.settings.public_origin or ""
        allow_loopback_origin = (not _po) or _is_loopback_url(_po)

        @self.app.middleware("http")
        async def csrf_origin_guard(request: Request, call_next):
            # CSRF-защита: мутирующие запросы к /api с чужого Origin отклоняем.
            # Браузер сам шлёт Origin на cross-site POST/PATCH/DELETE; запросы
            # без Origin (curl/не-браузер) не являются CSRF-вектором и проходят.
            if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path.startswith("/api/"):
                if not is_allowed_origin(
                    request.headers.get("origin"),
                    request.headers.get("host"),
                    allowed_origins,
                    allow_loopback=allow_loopback_origin,
                ):
                    return JSONResponse(
                        status_code=403, content={"detail": "cross-site request blocked"}
                    )
            return await call_next(request)

        @self.app.get("/api/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        # cookie_secure: explicit override wins, else derive from public_origin
        # scheme. Set web.cookie_secure: true in prod when nginx terminates
        # TLS upstream and public_origin is left as http://...
        # Если ни cookie_secure не задан, ни public_origin не указывает
        # явно на https — отказываем себе в догадках и выставляем False
        # ТОЛЬКО для loopback-биндинга. Иначе кука без Secure за прокси
        # может улететь в открытый канал.
        if self.settings.cookie_secure is not None:
            cookie_secure = self.settings.cookie_secure
        elif self.settings.public_origin.startswith("https://"):
            cookie_secure = True
        else:
            from src.utils.url_safety import is_loopback_host
            is_loopback = is_loopback_host(self.settings.host)
            if not is_loopback:
                logger.warning(
                    "web_cookie_secure_unsafe_default",
                    host=self.settings.host,
                    public_origin=self.settings.public_origin,
                    hint=(
                        "web.cookie_secure не задан и public_origin не https://; "
                        "set web.cookie_secure: true в config.yaml если за HTTPS-proxy."
                    ),
                )
            cookie_secure = False
        self.app.include_router(
            make_auth_router(
                bot_token=self.bot_token,
                bot_username=self.bot_username,
                allowed_user_ids=self.allowed_user_ids,
                jwt_secret=self.jwt_secret,
                jwt_ttl_days=self.settings.jwt_ttl_days,
                cookie_secure=cookie_secure,
                dev_bearer_token=self.dev_bearer_token,
                dev_login_enabled=self.settings.dev_login_enabled,
                magic_link_store=self.magic_link_store,
                session_manager=self.session_manager,
            )
        )
        # H-6: громкое предупреждение, если статический dev-bearer активен на
        # публичном (не-loopback) origin — рекомендуется выключить и перейти на
        # magic-link (web.dev_login_enabled: false).
        from src.utils.url_safety import is_loopback_url

        if (
            self.dev_bearer_token
            and self.settings.dev_login_enabled
            and self.settings.public_origin.startswith("https://")
            and not is_loopback_url(self.settings.public_origin)
        ):
            logger.warning(
                "dev_login_enabled_on_public_origin",
                public_origin=self.settings.public_origin,
                hint=(
                    "статический WEB_DEV_BEARER_TOKEN — слабое место (H-6); "
                    "после перехода на /weblogin magic-link установите "
                    "web.dev_login_enabled: false"
                ),
            )

        if self._ws_forwarder is not None and self.event_bus is not None:
            self.app.include_router(
                make_ws_router(
                    forwarder=self._ws_forwarder,
                    bus=self.event_bus,
                    jwt_secret=self.jwt_secret,
                    session_manager_factory=lambda: self.session_manager,
                    project_paths_provider=self._effective_project_paths,
                    allowed_user_ids=self.allowed_user_ids,
                    allowed_origins=allowed_origins,
                    allow_loopback_origin=allow_loopback_origin,
                    scratch_dir=self.scratch_dir,
                )
            )

        # REST: projects, sessions, message history, user settings
        self.app.include_router(
            make_projects_router(
                jwt_secret=self.jwt_secret,
                project_paths_provider=self._effective_project_paths,
                session_manager=self.session_manager,
                allowed_user_ids=self.allowed_user_ids,
            )
        )
        self.app.include_router(
            make_docs_router(
                jwt_secret=self.jwt_secret,
                project_paths_provider=self._effective_project_paths,
                session_manager=self.session_manager,
                allowed_user_ids=self.allowed_user_ids,
            )
        )
        self.app.include_router(
            make_files_router(
                jwt_secret=self.jwt_secret,
                project_paths_provider=self._effective_project_paths,
                session_manager=self.session_manager,
                allowed_user_ids=self.allowed_user_ids,
            )
        )
        # Подключения к сервисам (MCP) — регистрируем всегда: сам роутер
        # отвечает enabled:false / 503, когда connections_store is None
        # (фича выключена), так что не зависит от session_manager.
        self.app.include_router(
            make_connections_router(
                jwt_secret=self.jwt_secret,
                session_manager=self.session_manager,
                connections_store=self.connections_store,
                allowed_user_ids=self.allowed_user_ids,
            )
        )
        # Per-user Anthropic API-ключи (SP2) — регистрируем всегда: роутер сам
        # отвечает 501, когда api_key_store is None (фича выключена).
        self.app.include_router(
            make_apikey_router(
                jwt_secret=self.jwt_secret,
                session_manager=self.session_manager,
                api_key_store=self.api_key_store,
                allowed_user_ids=self.allowed_user_ids,
            )
        )
        if self.session_manager is not None:
            self.app.include_router(
                make_sessions_router(
                    jwt_secret=self.jwt_secret,
                    session_manager=self.session_manager,
                    project_paths_provider=self._effective_project_paths,
                    allowed_user_ids=self.allowed_user_ids,
                    running_tracker=self._running_tracker,
                )
            )
            self.app.include_router(
                make_settings_router(
                    jwt_secret=self.jwt_secret,
                    session_manager=self.session_manager,
                    allowed_user_ids=self.allowed_user_ids,
                )
            )
            self.app.include_router(
                make_model_router(
                    jwt_secret=self.jwt_secret,
                    session_manager=self.session_manager,
                    project_paths_provider=self._effective_project_paths,
                    allowed_user_ids=self.allowed_user_ids,
                )
            )
            self.app.include_router(
                make_uploads_router(
                    jwt_secret=self.jwt_secret,
                    session_manager=self.session_manager,
                    allowed_project_roots_provider=self._effective_project_paths,
                    allowed_user_ids=self.allowed_user_ids,
                )
            )
            self.app.include_router(
                make_admin_router(
                    jwt_secret=self.jwt_secret,
                    session_manager=self.session_manager,
                    allowed_user_ids=self.allowed_user_ids,
                    api_key_store=self.api_key_store,
                    projects_dir=self.projects_dir,
                )
            )
            # Self-service шеринг проекта: участники управляются владельцем
            # full-доступа (не только админом). Тот же источник корней проектов
            # (_effective_project_paths), что и у sessions/ws-роутеров, чтобы
            # авторизация по project_id совпадала во всём стеке.
            self.app.include_router(
                make_members_router(
                    jwt_secret=self.jwt_secret,
                    session_manager=self.session_manager,
                    allowed_user_ids=self.allowed_user_ids,
                    get_project_paths=self._effective_project_paths,
                )
            )

        # Статика фронта монтируется ПОСЛЕ всех /api-роутеров (SPA-fallback
        # перехватывает только не-API пути). No-op, если web/dist нет (dev).
        from pathlib import Path as _Path

        dist = _Path(__file__).resolve().parents[2] / "web" / "dist"
        mount_frontend(self.app, dist)

    def issue_magic_login_url(self, user_id: int) -> str:
        """Mint a one-time magic-link and return the full /login URL.

        Used by the Telegram bot's /weblogin command. Logs a warning
        when ``public_origin`` is not configured — the fallback URL
        only works for the user via an SSH-tunnel.
        """
        token = self.magic_link_store.create(user_id)
        origin = (self.settings.public_origin or "").rstrip("/")
        if not origin:
            logger.warning(
                "weblogin_no_public_origin",
                hint="link works only via SSH-tunnel; set web.public_origin for prod",
            )
            origin = f"http://127.0.0.1:{self.settings.port}"
        return f"{origin}/login?magic={token}"

    async def start(self) -> None:
        if not self.settings.enabled:
            logger.info("web_server_disabled")
            return
        if self._uvicorn_server is not None:
            return
        if not self.jwt_secret:
            logger.error(
                "web_jwt_secret_missing",
                hint="Set WEB_JWT_SECRET in .env or web.jwt_secret in config.yaml",
            )
            return

        # Bootstrap первого админа из .env (ADMIN_LOGIN/ADMIN_PASSWORD),
        # если такого пользователя ещё нет. Идемпотентно.
        if self.session_manager is not None:
            admin_login = (self.admin_login or "").strip()
            admin_password = self.admin_password or ""
            if admin_login and admin_password:
                from src.web.bootstrap import ensure_admin_user

                try:
                    await asyncio.to_thread(
                        ensure_admin_user,
                        self.session_manager,
                        login=admin_login,
                        password=admin_password,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("admin_bootstrap_failed", error=str(exc))

        config = uvicorn.Config(
            self.app,
            host=self.settings.host,
            port=self.settings.port,
            log_level="info",
            lifespan="on",
            loop="asyncio",
            access_log=False,
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._serve_task = asyncio.create_task(self._uvicorn_server.serve())
        logger.info(
            "web_server_started",
            host=self.settings.host,
            port=self.settings.port,
        )

    async def stop(self) -> None:
        if self._uvicorn_server is None:
            return
        self._uvicorn_server.should_exit = True
        if self._serve_task is not None:
            try:
                await self._serve_task
            except asyncio.CancelledError:
                pass
            self._serve_task = None
        self._uvicorn_server = None
        logger.info("web_server_stopped")
