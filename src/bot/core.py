"""Bot core - initialization and setup."""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from aiogram.client.session.aiohttp import AiohttpSession

import structlog

from src.bot.handlers import (
    setup_apikey_handlers,
    setup_callback_handlers,
    setup_command_handlers,
    setup_connect_handlers,
    setup_file_handlers,
    setup_message_handlers,
)
from src.bot.middleware import AuthMiddleware, SecurityMiddleware
from src.claude import ClaudeBridge, SessionManager, SessionStatus, sync_commands
from src.config import Settings
from src.event_bus import ClaudeEventRelay, EventBus, WebhookEventHandler, WebhookTriggered
from src.scheduler import CronScheduler
from src.utils import ResponseStreamer
from src.utils.formatter import format_for_telegram, strip_html_tags
from src.webhooks import WebhookAPIServer, WebhookEvent

logger = structlog.get_logger()


def plan_sdk_session_restore(sessions, existing_topic_ids):
    """Решает, как восстановить session_id (SDK-транспорт) на старте бота.

    Возвращает ``(restore_map, fork_pending)``:

    - ``restore_map`` — ``{topic_id: session_id}`` для резюме in-place;
    - ``fork_pending`` — список ``topic_id``, чей первый resume должен
      форкнуть сессию (``--fork-session``), а не продолжать in-place.

    Зачем форк: deeplink «Продолжить в Telegram» оставляет веб-сессию и новый
    Telegram-топик с ОДНИМ session_id. В рамках процесса это решает in-memory
    ``ClaudeBridge._fork_pending``, но при рестарте бота тот флаг теряется, а
    общий session_id остаётся в БД — и resume копии без форка дописывал бы
    общий transcript. Коллизия уже хранится в БД (один session_id у нескольких
    топиков), поэтому детектируем её здесь: копию (положительный topic_id —
    Telegram-топик; веб-сессии всегда в отрицательном диапазоне) форкаем, а
    оригинал (веб, отрицательный topic_id) резюмируем in-place. Уже живые в
    памяти топики (``existing_topic_ids``) не трогаем.
    """
    sid_counts = Counter(s.session_id for s in sessions if s.session_id)
    restore_map: dict[int, str] = {}
    fork_pending: list[int] = []
    for session in sessions:
        if not session.session_id or session.topic_id in existing_topic_ids:
            continue
        if sid_counts[session.session_id] > 1 and session.topic_id >= 0:
            fork_pending.append(session.topic_id)
        else:
            restore_map[session.topic_id] = session.session_id
    return restore_map, fork_pending


class TelegramClaudeBot:
    """Main bot class."""

    def __init__(self, settings: Settings):
        self.settings = settings

        # Create session WITHOUT proxy — Telegram API
        # is reachable via VPN directly, while HTTP_PROXY
        # env var is used by Claude CLI for Anthropic API.
        session = AiohttpSession()
        # Override: don't use env proxy for Telegram
        session._proxy = None

        # Initialize bot
        self.bot = Bot(
            token=settings.get_bot_token(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=session,
        )

        # Initialize dispatcher (MemoryStorage backs the /connect FSM)
        self.dp = Dispatcher(storage=MemoryStorage())

        # Initialize components
        self.session_manager = SessionManager(
            storage_path=settings.get_session_database_path()
        )
        self.claude_bridge = ClaudeBridge(
            transport=settings.claude.transport,
            permission_mode=settings.claude.permission_mode,
            timeout_minutes=settings.claude.timeout_minutes,
            max_turns=settings.claude.max_turns,
            idle_timeout_seconds=settings.claude.idle_timeout_seconds,
            # Тот же scratch, что у веб-сервера (ниже) — чтобы no-project
            # сессии бота и веба совпадали по cwd (deeplink-resume).
            scratch_dir=settings.get_scratch_dir(),
            # Публичный адрес веба → системный промпт Claude (issue #2): чтобы
            # на «какой адрес» отвечал публичным URL, а не 127.0.0.1.
            web_public_origin=(
                settings.web.public_origin if settings.web.enabled else None
            ),
            # Fail-CLOSED OS-джейл: если require_jail — confine'нутая сессия не
            # стартует без bwrap/userns (иначе best-effort, соло self-host).
            require_jail=settings.claude.require_jail,
        )
        self.event_bus = EventBus()
        # Built before the relay so the relay can resolve per-user MCP servers
        # from it. Also passed to the connect/message handlers in _setup_handlers.
        from src.connections.service import build_connections_store

        self.connections_store = build_connections_store(self.settings)
        # SP2 per-user Anthropic keys. None when CONNECTIONS_SECRET_KEY is unset
        # (feature stays dormant); backs the /apikey command's set/delete flow.
        from src.apikeys.service import build_api_key_store

        self.api_key_store = build_api_key_store(self.settings)
        self.claude_event_relay = ClaudeEventRelay(
            bus=self.event_bus,
            claude_bridge=self.claude_bridge,
            connections_store=self.connections_store,
            api_key_store=self.api_key_store,
            settings=self.settings,
        )
        self.streamer = ResponseStreamer(
            bot=self.bot,
            max_message_length=settings.limits.max_message_length,
            log_update_interval_ms=settings.display.log_update_interval_ms,
            code_as_file_threshold=settings.limits.code_as_file_threshold,
        )
        self.cron_scheduler = CronScheduler(
            bot=self.bot,
            settings=self.settings,
            session_manager=self.session_manager,
            claude_bridge=self.claude_bridge,
            streamer=self.streamer,
        )
        self.webhook_server = WebhookAPIServer(
            settings.webhooks,
            on_event=self._handle_webhook_event,
        )
        self.webhook_event_handler = WebhookEventHandler(
            bus=self.event_bus,
            bot=self.bot,
            claude_bridge=self.claude_bridge,
            streamer=self.streamer,
            session_manager=self.session_manager,
            settings=settings,
        )

        # Web UI subsystem (local imports — avoids loading FastAPI/uvicorn
        # when web.enabled is False)
        from src.web.message_store import MessageHistoryPersister
        from src.web.server import WebServer

        self.message_history_persister = MessageHistoryPersister(
            bus=self.event_bus,
            session_manager=self.session_manager,
        )
        self.web_server = WebServer(
            settings=settings.web,
            allowed_user_ids=settings.get_allowed_user_ids(),
            bot_username=settings.get_telegram_bot_username(),
            session_manager=self.session_manager,
            event_bus=self.event_bus,
            bot_token=settings.get_bot_token(),
            jwt_secret=settings.get_web_jwt_secret(),
            dev_bearer_token=settings.get_web_dev_bearer_token(),
            # Тот же список, что у web-only входа: ограничение light не
            # должно зависеть от того, подключён Telegram или нет.
            project_paths=settings.get_light_project_paths(),
            admin_login=settings.get_admin_login(),
            admin_password=settings.get_admin_password(),
            scratch_dir=settings.get_scratch_dir(),
            connections_store=self.connections_store,
            api_key_store=self.api_key_store,
        )

        # Setup middleware
        self._setup_middleware()

        # Setup handlers
        self._setup_handlers()

    def _setup_middleware(self) -> None:
        """Setup middleware."""
        allowed_users = self.settings.get_allowed_user_ids()

        if not allowed_users:
            logger.warning("no_allowed_users_configured")

        # Auth middleware (runs first). session_manager передаётся, чтобы
        # апсертить строку users (+username) при взаимодействии — иначе
        # приглашение участника по @username / числовому id отдаёт 404 (H-1/M-10).
        self.dp.message.middleware(
            AuthMiddleware(allowed_users, session_manager=self.session_manager)
        )
        self.dp.callback_query.middleware(
            AuthMiddleware(allowed_users, session_manager=self.session_manager)
        )

        # Security middleware (runs after auth). Регистрируем БЕЗУСЛОВНО (L-14):
        # даже без настроенных проектов prompt-фильтр (validate_user_message)
        # должен работать. С пустым allowed_roots containment-проверки —
        # no-op, но фильтр опасных инструкций активен.
        project_paths = self.settings.get_light_project_paths()
        security = SecurityMiddleware(
            allowed_roots=project_paths or [],
            audit_log_path=self.settings.get_session_database_path().parent / "security.log",
        )
        self.dp.message.middleware(security)

    def _setup_handlers(self) -> None:
        """Setup message and callback handlers."""
        # Commands first (higher priority)
        setup_command_handlers(
            self.dp,
            settings=self.settings,
            session_manager=self.session_manager,
            claude_bridge=self.claude_bridge,
            web_server=self.web_server,
        )

        # Callbacks
        setup_callback_handlers(
            self.dp,
            settings=self.settings,
            session_manager=self.session_manager,
            claude_bridge=self.claude_bridge,
            bot=self.bot,
        )

        # File handlers
        setup_file_handlers(
            self.dp,
            settings=self.settings,
            session_manager=self.session_manager,
            claude_bridge=self.claude_bridge,
            streamer=self.streamer,
            bot=self.bot,
            event_bus=self.event_bus,
            api_key_store=self.api_key_store,
        )

        # Connect flow (/connect) — MUST come before the catch-all message
        # router so its FSM awaiting_token handler intercepts the token
        # message instead of it going to Claude. self.connections_store is
        # created in __init__ (before the relay).
        setup_connect_handlers(
            self.dp,
            connections_store=self.connections_store,
            bot=self.bot,
        )

        # /apikey FSM (set/delete per-user Anthropic key) — like /connect, MUST
        # come before the catch-all message router so its waiting_for_key
        # handler intercepts the pasted key instead of it reaching Claude.
        setup_apikey_handlers(
            self.dp,
            api_key_store=self.api_key_store,
            bot=self.bot,
        )

        # Message handler last (catch-all)
        setup_message_handlers(
            self.dp,
            settings=self.settings,
            session_manager=self.session_manager,
            claude_bridge=self.claude_bridge,
            streamer=self.streamer,
            bot=self.bot,
            event_bus=self.event_bus,
            connections_store=self.connections_store,
            api_key_store=self.api_key_store,
        )

    async def _handle_webhook_event(self, event: WebhookEvent) -> None:
        """Handle a webhook event."""
        logger.info(
            "webhook_event_received",
            source=event.source,
            kind=event.kind,
            repository=event.repository,
            delivery_id=event.delivery_id,
        )
        await self.event_bus.publish(
            WebhookTriggered(
                source=event.source,
                kind=event.kind,
                payload=event.payload,
                delivery_id=event.delivery_id,
                repository=event.repository,
                action=event.action,
                ref=event.ref,
            )
        )

    async def _restore_session_ids_from_db(self) -> None:
        """Restore bridge session_id mapping from DB.

        For tmux transport: only restore for discovered tmux sessions.
        For SDK transport: restore all session IDs so that ``--resume``
        works after a bot restart.
        """
        sessions = await self.session_manager.async_get_all_sessions()
        if not sessions:
            return

        if self.claude_bridge.transport == "tmux":
            tmux_topics = self.claude_bridge.discovered_tmux_topics
            if not tmux_topics:
                return
            mapping: dict[int, str] = {}
            for session in sessions:
                if session.topic_id in tmux_topics and session.session_id:
                    mapping[session.topic_id] = session.session_id
            if mapping:
                self.claude_bridge.restore_session_ids(mapping)
        else:
            # SDK transport — restore all session IDs. Deeplink-копии
            # (общий session_id у веб-сессии и Telegram-топика) помечаются на
            # форк вместо resume in-place, чтобы после рестарта бота они не
            # начали дописывать общий transcript (см. plan_sdk_session_restore).
            restore_map, fork_pending = plan_sdk_session_restore(
                sessions, set(self.claude_bridge._session_ids)
            )
            self.claude_bridge._session_ids.update(restore_map)
            for topic_id in fork_pending:
                self.claude_bridge.mark_fork_pending(topic_id)
            if restore_map or fork_pending:
                logger.info(
                    "session_ids_restored_from_db",
                    count=len(restore_map),
                    fork_pending=len(fork_pending),
                    transport="sdk",
                )

    async def _recover_tmux_sessions(self) -> None:
        """Check each discovered tmux session: deliver result or monitor.

        For sessions that Claude has already finished (``result`` event found
        in the tmux pane), the bot delivers the response to Telegram and
        marks the session as ``recovered``.  For sessions still running, a
        background monitoring task is spawned.
        """
        tmux_topics = self.claude_bridge.discovered_tmux_topics
        if not tmux_topics:
            return

        sessions = await self.session_manager.async_get_all_sessions()
        session_map = {s.topic_id: s for s in sessions}

        for topic_id, session_name in tmux_topics.items():
            db_session = session_map.get(topic_id)
            if not db_session:
                logger.warning(
                    "tmux_session_no_db_record",
                    topic_id=topic_id,
                    session_name=session_name,
                )
                continue

            # Skip already-recovered sessions
            if db_session.status == SessionStatus.RECOVERED:
                logger.debug("tmux_session_already_recovered", topic_id=topic_id)
                continue

            # Skip sessions without chat_id -- we can't deliver
            if not db_session.chat_id:
                logger.warning(
                    "tmux_session_no_chat_id",
                    topic_id=topic_id,
                    session_name=session_name,
                )
                continue

            # Capture tmux pane content
            result = await self.claude_bridge._run_tmux(
                "capture-pane",
                "-pt",
                session_name,
                "-S",
                "-2000",
                check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "tmux_capture_failed",
                    topic_id=topic_id,
                    session_name=session_name,
                    stderr=result.stderr,
                )
                continue

            # Parse JSON lines looking for a result event
            finished, response_text, usage_data = self._parse_tmux_output(result.stdout)

            if finished:
                await self._deliver_recovered_result(
                    topic_id=topic_id,
                    chat_id=db_session.chat_id,
                    session_name=session_name,
                    response_text=response_text,
                    usage_data=usage_data,
                    db_session=db_session,
                )
            else:
                # Claude still running -- spawn background monitor
                asyncio.create_task(
                    self._monitor_running_tmux_session(
                        topic_id=topic_id,
                        chat_id=db_session.chat_id,
                        session_name=session_name,
                        db_session=db_session,
                    )
                )

        logger.info("tmux_recovery_complete", topics=len(tmux_topics))

    @staticmethod
    def _parse_tmux_output(
        output: str,
    ) -> tuple[bool, str, dict | None]:
        """Parse tmux pane output for a Claude result event.

        Returns (finished, response_text, usage_data).
        """
        text_parts: list[str] = []
        usage_data: dict | None = None
        finished = False

        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = data.get("type", "")

            # Collect text from text_delta stream events
            if event_type == "stream_event":
                event = data.get("event", {}) or {}
                delta = event.get("delta", {}) or {}
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        text_parts.append(text)

            # result event = Claude finished
            elif event_type == "result":
                finished = True
                result_text = data.get("result", "")
                usage = data.get("usage", {}) or {}
                cost = data.get("total_cost_usd", 0.0)
                usage_data = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_tokens": usage.get(
                        "cache_read_input_tokens",
                        usage.get("cache_read_tokens", 0),
                    ),
                    "cache_creation_tokens": usage.get(
                        "cache_creation_input_tokens",
                        usage.get("cache_creation_tokens", 0),
                    ),
                    "cost_usd": float(cost or 0.0),
                }
                # If no streaming text was collected, use the result field
                if not text_parts and result_text:
                    text_parts.append(result_text)

        return finished, "".join(text_parts), usage_data

    async def _deliver_recovered_result(
        self,
        *,
        topic_id: int,
        chat_id: int,
        session_name: str,
        response_text: str,
        usage_data: dict | None,
        db_session,
    ) -> None:
        """Send a recovered Claude result to Telegram and clean up."""
        if not response_text.strip():
            response_text = "(Claude finished but produced no text output)"

        # Format for Telegram
        try:
            formatted = format_for_telegram(response_text)
        except Exception:
            formatted = response_text

        header = "<b>[Recovered after bot restart]</b>\n\n"
        marker = "\n\n<i>...truncated</i>"
        max_body = 4096 - len(header)

        # A raw-character slice of formatted HTML can cut mid-tag or
        # mid-attribute and break Telegram's entity parser. Truncate the
        # raw text first, then re-format.
        if len(formatted) > max_body:
            ratio = max_body / len(formatted)
            budget = max(0, int(len(response_text) * ratio * 0.9) - len(marker))
            truncated_raw = response_text[:budget].rstrip()
            try:
                formatted = format_for_telegram(truncated_raw)
            except Exception:
                formatted = truncated_raw
            if len(formatted) + len(marker) <= max_body:
                formatted += marker
            else:
                formatted = strip_html_tags(response_text)[: max_body - len(marker)] + marker

        message_text = header + formatted

        try:
            await self.bot.send_message(
                chat_id=chat_id,
                message_thread_id=topic_id,
                text=message_text,
                parse_mode="HTML",
            )
            logger.info(
                "recovered_result_delivered",
                topic_id=topic_id,
                chat_id=chat_id,
                text_length=len(response_text),
            )
        except Exception as exc:
            # Fallback: try without HTML parsing in case of formatting issues
            logger.warning(
                "recovered_delivery_html_failed",
                topic_id=topic_id,
                error=str(exc),
            )
            try:
                plain = strip_html_tags(message_text)
                await self.bot.send_message(
                    chat_id=chat_id,
                    message_thread_id=topic_id,
                    text=plain[:4096],
                )
            except Exception as exc2:
                logger.error(
                    "recovered_delivery_failed",
                    topic_id=topic_id,
                    error=str(exc2),
                )
                return

        # Record usage if available
        if usage_data:
            await self.session_manager.async_add_usage(
                topic_id,
                usage_data.get("input_tokens", 0),
                usage_data.get("output_tokens", 0),
                cache_read_tokens=usage_data.get("cache_read_tokens", 0),
                cache_creation_tokens=usage_data.get("cache_creation_tokens", 0),
                cost_usd=usage_data.get("cost_usd", 0.0),
                command_type="recovered",
            )

        # Mark as recovered so we don't re-deliver on next restart
        await self.session_manager.async_set_status(topic_id, SessionStatus.RECOVERED)

        # Kill the tmux session -- Claude has already finished
        await self.claude_bridge._run_tmux(
            "kill-session", "-t", session_name, check=False
        )
        logger.info("recovered_tmux_session_killed", session_name=session_name)

    async def _monitor_running_tmux_session(
        self,
        *,
        topic_id: int,
        chat_id: int,
        session_name: str,
        db_session,
    ) -> None:
        """Background task: poll a still-running tmux session until it finishes.

        When Claude completes, delivers the result to Telegram.
        """
        logger.info(
            "monitoring_running_tmux",
            topic_id=topic_id,
            session_name=session_name,
        )
        poll_interval = self.claude_bridge.capture_poll_interval_seconds
        timeout = self.claude_bridge.timeout_seconds

        elapsed = 0.0
        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            result = await self.claude_bridge._run_tmux(
                "capture-pane",
                "-pt",
                session_name,
                "-S",
                "-2000",
                check=False,
            )
            if result.returncode != 0:
                # tmux session gone -- Claude was killed externally
                logger.warning(
                    "monitored_tmux_session_gone",
                    topic_id=topic_id,
                    session_name=session_name,
                )
                await self.session_manager.async_set_status(topic_id, SessionStatus.ERROR)
                return

            finished, response_text, usage_data = self._parse_tmux_output(result.stdout)
            if finished:
                await self._deliver_recovered_result(
                    topic_id=topic_id,
                    chat_id=chat_id,
                    session_name=session_name,
                    response_text=response_text,
                    usage_data=usage_data,
                    db_session=db_session,
                )
                return

        # Timed out waiting
        logger.warning(
            "monitored_tmux_session_timeout",
            topic_id=topic_id,
            session_name=session_name,
            elapsed=elapsed,
        )
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                message_thread_id=topic_id,
                text="<b>[Recovery timeout]</b>\n\nClaude session is still running but exceeded the monitoring timeout. You can send a new message to continue.",
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("recovery_timeout_notify_failed", error=str(exc))

    async def start(self) -> None:
        """Start the bot."""
        logger.info(
            "bot_starting",
            allowed_users=self.settings.get_allowed_user_ids(),
            projects_dir=str(self.settings.get_projects_directory()),
        )

        # Start persistent process cleanup loop
        await self.claude_bridge.start_cleanup_loop()

        # Bootstrap the owner as admin on a fresh DB (no admins yet) so a
        # self-host operator is a real admin from first launch — model change,
        # admin panel and is_bot_privileged all work without manual SQL. If any
        # admin already exists (e.g. prod, set by hand) this is a no-op, so
        # later-added whitelist users (students) are never over-granted.
        # L-2: владелец берётся из явного OWNER_USER_ID (установщик пишет туда id,
        # который оператор указал как свой), фолбэк — первый ALLOWED_USER_ID.
        # Иначе, если учитель вписал ученика раньше себя, админом молча стал бы
        # ученик.
        owner_id = self.settings.get_owner_user_id()
        if owner_id is not None:
            try:
                promoted = await asyncio.to_thread(
                    self.session_manager.ensure_owner_admin, owner_id
                )
                if promoted:
                    logger.info("bootstrapped_owner_as_admin", user_id=owner_id)
            except Exception as exc:  # noqa: BLE001 — never block startup on this
                logger.warning("owner_admin_bootstrap_failed", error=str(exc))

        # Restore session IDs from DB for discovered tmux sessions
        await self._restore_session_ids_from_db()

        # Recover tmux sessions: deliver finished results, monitor running ones
        await self._recover_tmux_sessions()

        # ------------------------------------------------------------------
        # Local subsystems — start BEFORE any Telegram API call so the web
        # subsystem keeps running even when Telegram is unreachable (bad
        # token, offline network, etc.).
        # ------------------------------------------------------------------
        await self.cron_scheduler.start()
        # Persister subscribes to the bus BEFORE the web server accepts WS
        # clients, so no events are missed if a client connects mid-startup.
        await self.message_history_persister.start()
        await self.web_server.start()
        await self.webhook_server.start()

        # ------------------------------------------------------------------
        # Telegram side — guarded so a connect failure doesn't take the web
        # subsystem down with it. Шаги делятся на две части:
        # 1) init (sync_commands + get_me + delete_webhook) — делается один
        #    раз; если первый init упал, ждём и пробуем заново, но без
        #    повторного запуска делается после однократного успеха.
        # 2) start_polling — основной цикл, ретраится с backoff. На каждой
        #    итерации init НЕ перевыполняется, чтобы не дёргать Bot API
        #    лишний раз и не словить rate-limit на setMyCommands.
        # ------------------------------------------------------------------
        base_retry_delay = 30
        retry_delay = base_retry_delay
        max_retry_delay = 600
        # A drop after polling has been healthy for at least this long is a
        # fresh transient failure, not rapid flapping — reset the backoff so
        # reconnect is quick instead of inheriting a stale large delay.
        reset_backoff_after_seconds = 60
        initialized = False
        while True:
            polling_started_at: float | None = None
            try:
                if not initialized:
                    await sync_commands(self.bot)
                    me = await self.bot.get_me()
                    logger.info(
                        "bot_info",
                        bot_id=me.id,
                        username=f"@{me.username}",
                        name=me.full_name,
                    )
                    await self.bot.delete_webhook(drop_pending_updates=True)
                    initialized = True
                logger.info("✅ bot_ready — polling for updates...")
                polling_started_at = time.monotonic()
                await self.dp.start_polling(self.bot)
                # start_polling returning normally means the dispatcher
                # was stopped from elsewhere — exit the retry loop.
                return
            except asyncio.CancelledError:
                # Normal shutdown path — propagate.
                raise
            except Exception as exc:
                # Reset backoff if polling had been running for a while: a
                # disconnect after a long healthy run shouldn't reuse the
                # escalated delay from earlier flapping (CR3-14).
                if (
                    polling_started_at is not None
                    and time.monotonic() - polling_started_at >= reset_backoff_after_seconds
                ):
                    retry_delay = base_retry_delay
                logger.warning(
                    "telegram_connect_failed",
                    error=str(exc),
                    exc_class=type(exc).__name__,
                    retry_in_seconds=retry_delay,
                    initialized=initialized,
                    hint="Web subsystem continues running; Telegram features disabled until reconnect.",
                )
                try:
                    await asyncio.sleep(retry_delay)
                except asyncio.CancelledError:
                    raise
                retry_delay = min(retry_delay * 2, max_retry_delay)

    async def stop(self) -> None:
        """Stop the bot."""
        logger.info("🛑 bot_stopping — shutting down...")
        await self.cron_scheduler.stop()
        # Stop web server first (closes WS clients), then drain persister
        await self.web_server.stop()
        await self.message_history_persister.stop()
        await self.webhook_server.stop()
        await self.claude_bridge.shutdown()
        await self.bot.session.close()
        logger.info("bot_stopped")


async def run_bot(settings: Settings) -> None:
    """Run the bot."""
    bot = TelegramClaudeBot(settings)

    try:
        await bot.start()
    except asyncio.CancelledError:
        pass
    finally:
        await bot.stop()
