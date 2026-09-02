"""Безопасность: бот-команды /config и /permissions НЕ должны раскрывать
секреты из ~/.claude/settings.json любому whitelisted-юзеру (в т.ч. не-админу).

Аудит: веб уже редактирован через native_commands.render_*_report(), а бот
дампил СЫРОЙ json (env-прокси-креды, apiKeyHelper, MCP-токены) и полный
allow/deny firewall. Здесь бот переиспользует те же безопасные рендереры.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import src.claude.claude_settings as cs
import src.claude.native_commands as nc
from src.bot.handlers import messages

# settings.json со СЕКРЕТАМИ и firewall-паттернами.
SECRET_SETTINGS = {
    "model": "opus",
    "permissions": {
        "defaultMode": "bypassPermissions",
        "allow": ["Read(*)"],
        "deny": ["Read(.env)", "Read(**/*credentials*)"],
    },
    "env": {"HTTPS_PROXY": "http://user:SUPERSECRETPW@proxy:8080"},
    "apiKeyHelper": "echo sk-ant-LEAKED",
    "mcpServers": {"notion": {"command": "npx", "env": {"NOTION_TOKEN": "ntn_SECRET"}}},
}


def _message():
    return SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        message_thread_id=91,
        answer=AsyncMock(),
    )


class BotConfigRedactionTests(unittest.IsolatedAsyncioTestCase):
    def _patch_settings(self, data):
        # messages._handle_native_command делает локальный
        # `from src.claude.claude_settings import read_claude_settings` →
        # патчим cs.  render_*_report() зовут nc.read_claude_settings
        # (связано на import-time) → патчим и nc.
        p_cs = patch.object(cs, "read_claude_settings", return_value=data)
        p_nc = patch.object(nc, "read_claude_settings", return_value=data)
        p_cs.start()
        p_nc.start()
        self.addCleanup(p_cs.stop)
        self.addCleanup(p_nc.stop)

    async def test_config_shows_safe_subset(self):
        self._patch_settings(SECRET_SETTINGS)
        msg = _message()
        handled = await messages._handle_native_command("/config", "/config", msg)
        assert handled is True
        text = msg.answer.await_args.args[0]
        # Безопасное подмножество ДОЛЖНО быть.
        assert "opus" in text  # модель
        assert "bypassPermissions" in text  # режим разрешений
        assert "notion" in text  # ИМЯ MCP-сервера

    async def test_config_never_leaks_secrets(self):
        self._patch_settings(SECRET_SETTINGS)
        msg = _message()
        await messages._handle_native_command("/config", "/config", msg)
        text = msg.answer.await_args.args[0]
        for leak in ("SUPERSECRETPW", "sk-ant-LEAKED", "ntn_SECRET",
                     "apiKeyHelper", "NOTION_TOKEN", "HTTPS_PROXY"):
            assert leak not in text, f"утечка секрета: {leak!r}"

    async def test_config_empty_still_short_circuits(self):
        self._patch_settings({})
        msg = _message()
        handled = await messages._handle_native_command("/config", "/config", msg)
        assert handled is True
        text = msg.answer.await_args.args[0]
        assert "пуст" in text.lower()

    async def test_permissions_shows_mode_and_counts(self):
        self._patch_settings(SECRET_SETTINGS)
        msg = _message()
        handled = await messages._handle_native_command("/permissions", "/permissions", msg)
        assert handled is True
        text = msg.answer.await_args.args[0]
        assert "bypassPermissions" in text  # режим
        assert "1" in text and "2" in text  # allow 1 / deny 2 — счётчики

    async def test_permissions_never_leaks_patterns(self):
        self._patch_settings(SECRET_SETTINGS)
        msg = _message()
        await messages._handle_native_command("/permissions", "/permissions", msg)
        text = msg.answer.await_args.args[0]
        assert "Read(.env)" not in text
        assert ".env" not in text
        assert "credentials" not in text
