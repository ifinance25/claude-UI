"""Запуск только веб-интерфейса, без Telegram-бота.

Light-версия отдаётся как веб-продукт: человек ставит её на свой сервер и
работает через браузер. Основной вход `python -m src.main` поднимает Telegram и
без токена бота отказывается стартовать — этот вход собирает тот же стек без
Telegram: сессии, шина событий, мост к Claude Code и веб-сервер.

Запуск из корня проекта:

    python scripts/run_web.py
    python scripts/run_web.py --port 8600     # если порт из конфига занят

Конфигурация — та же, что у основного входа: config/config.yaml + .env.
Логин и пароль первого администратора берутся из ADMIN_LOGIN / ADMIN_PASSWORD
(WebServer заводит его при старте, если такого пользователя ещё нет).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


def _load_settings():
    from src.config import Settings

    config_path = Path("config/config.yaml")
    if not config_path.exists():
        config_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    return Settings.from_yaml(config_path)


async def _serve(host: str | None, port: int | None) -> int:
    from src.apikeys.service import build_api_key_store
    from src.claude.bridge import ClaudeBridge
    from src.claude.session import SessionManager
    from src.connections.service import build_connections_store
    from src.event_bus import ClaudeEventRelay, EventBus
    from src.web.message_store import MessageHistoryPersister
    from src.web.server import WebServer

    settings = _load_settings()
    # Аргументы командной строки перекрывают конфиг: на Windows дефолтный порт
    # может попасть в системный резерв (netsh excludedportrange → winerror 10013).
    if host:
        settings.web.host = host
    if port:
        settings.web.port = port
    if not settings.web.enabled:
        logger.error(
            "web_disabled",
            hint="Поставьте web.enabled: true в config/config.yaml — этот вход поднимает только веб.",
        )
        return 1

    # Light работает с одним проектом: выбор проектов и папок из интерфейса
    # убран, Sidebar создаёт чаты в первом. Решение живёт в одном месте —
    # get_light_project_paths, — потому что тот же список нужен и Telegram-входу
    # (src/bot/core.py). Когда логика была здесь, подключение бота молча
    # возвращало в браузер полный список проектов.
    project_paths = settings.get_light_project_paths()

    session_manager = SessionManager(storage_path=settings.get_session_database_path())
    bus = EventBus()
    connections_store = build_connections_store(settings)
    api_key_store = build_api_key_store(settings)

    bridge = ClaudeBridge(
        transport=settings.claude.transport,
        permission_mode=settings.claude.permission_mode,
        timeout_minutes=settings.claude.timeout_minutes,
        max_turns=settings.claude.max_turns,
        idle_timeout_seconds=settings.claude.idle_timeout_seconds,
        scratch_dir=settings.get_scratch_dir(),
        web_public_origin=settings.web.public_origin,
        require_jail=settings.claude.require_jail,
    )
    relay = ClaudeEventRelay(
        bus=bus,
        claude_bridge=bridge,
        connections_store=connections_store,
        api_key_store=api_key_store,
        settings=settings,
    )
    # Пишет ленту сообщений в БД — без него история чата не переживает
    # перезагрузку страницы.
    persister = MessageHistoryPersister(bus=bus, session_manager=session_manager)

    server = WebServer(
        settings=settings.web,
        allowed_user_ids=settings.get_allowed_user_ids(),
        bot_username=settings.get_telegram_bot_username(),
        session_manager=session_manager,
        event_bus=bus,
        bot_token=settings.get_bot_token(),
        jwt_secret=settings.get_web_jwt_secret(),
        dev_bearer_token=settings.get_web_dev_bearer_token(),
        project_paths=project_paths,
        admin_login=settings.get_admin_login(),
        admin_password=settings.get_admin_password(),
        scratch_dir=settings.get_scratch_dir(),
        connections_store=connections_store,
        api_key_store=api_key_store,
    )

    await persister.start()
    await server.start()
    logger.info(
        "web_only_started",
        url=f"http://{settings.web.host}:{settings.web.port}",
        projects=len(settings.get_project_paths()),
    )
    try:
        await asyncio.Event().wait()  # до Ctrl+C
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()
        await persister.stop()
        relay.close()
        await session_manager.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Vels Claude Light — только веб, без Telegram")
    parser.add_argument("--host", default=None, help="переопределяет web.host из config.yaml")
    parser.add_argument("--port", type=int, default=None, help="переопределяет web.port из config.yaml")
    args = parser.parse_args()
    try:
        return asyncio.run(_serve(args.host, args.port))
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
        return 0


if __name__ == "__main__":
    sys.exit(main())
