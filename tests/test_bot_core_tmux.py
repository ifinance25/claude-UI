from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import ANY, patch

from src.bot.core import TelegramClaudeBot
from src.config.settings import Settings


class BotCoreTmuxWiringTests(unittest.TestCase):
    def test_bot_initializes_bridge_with_tmux_transport(self) -> None:
        settings = Settings(
            telegram={"token": "token"},
            claude={
                "permission_mode": "bypassPermissions",
                "timeout_minutes": 30,
                "max_turns": 100,
                "idle_timeout_seconds": 60,
                "transport": "tmux",
            },
            webhooks={"enabled": False},
        )

        with patch("src.bot.core.AiohttpSession", return_value=SimpleNamespace(_proxy=None)), patch(
            "src.bot.core.Bot",
        ), patch(
            "src.bot.core.Dispatcher",
        ), patch(
            "src.bot.core.SessionManager",
        ), patch(
            "src.bot.core.ClaudeBridge",
        ) as bridge_cls, patch(
            "src.bot.core.ResponseStreamer",
        ), patch(
            "src.bot.core.WebhookAPIServer",
        ), patch.object(
            TelegramClaudeBot,
            "_setup_middleware",
            lambda self: None,
        ), patch.object(
            TelegramClaudeBot,
            "_setup_handlers",
            lambda self: None,
        ):
            TelegramClaudeBot(settings)

        bridge_cls.assert_called_once_with(
            permission_mode="bypassPermissions",
            timeout_minutes=30,
            max_turns=100,
            idle_timeout_seconds=60,
            transport="tmux",
            scratch_dir=ANY,
            web_public_origin=ANY,
            require_jail=False,
        )
