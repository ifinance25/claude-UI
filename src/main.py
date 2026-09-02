"""Main entry point for Telegram Claude Code Bot."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import structlog

# Configure stdlib logging — set level to DEBUG so structlog's
# filter_by_level doesn't silently drop INFO/DEBUG messages.
logging.basicConfig(
    format="%(message)s",
    stream=sys.stderr,
    level=logging.INFO,
)

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


def main() -> int:
    """Main entry point."""
    from src.bot.core import run_bot
    from src.config import Settings

    # Determine config path
    config_path = Path("config/config.yaml")
    if not config_path.exists():
        # Try parent directory (if running from src/)
        config_path = Path("../config/config.yaml")

    # Load settings
    try:
        settings = Settings.from_yaml(config_path)
    except Exception as e:
        logger.error("failed_to_load_settings", error=str(e))
        return 1

    # Validate settings
    if not settings.get_bot_token():
        logger.error(
            "no_bot_token",
            hint="Set TELEGRAM_BOT_TOKEN env variable or telegram.token in config.yaml",
        )
        return 1

    if not settings.get_allowed_user_ids():
        logger.warning(
            "no_allowed_users",
            hint="Set ALLOWED_USER_IDS env variable or security.allowed_user_ids in config.yaml",
        )

    # Log startup info
    logger.info(
        "starting_telegram_claude_code_bot",
        projects_dir=str(settings.get_projects_directory()),
        projects_count=len(settings.get_project_paths()),
        permission_mode=settings.claude.permission_mode,
    )

    # Run the bot
    try:
        asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        logger.info("shutdown_requested")
    except Exception as e:
        logger.error("bot_crashed", error=str(e))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
