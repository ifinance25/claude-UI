"""Чистый текстовый слой нативных (TUI-only) слеш-команд для веба и бота."""
from __future__ import annotations

from unittest.mock import patch

from src.claude import native_commands as nc


def test_config_report_includes_model():
    with patch.object(nc, "read_claude_settings", return_value={"model": "claude-opus-5"}):
        text = nc.render_config_report()
    assert "claude-opus-5" in text
    assert "Config" in text or "Конфиг" in text


def test_config_report_empty():
    with patch.object(nc, "read_claude_settings", return_value={}):
        text = nc.render_config_report()
    assert "пуст" in text.lower()


def test_config_report_redacts_env_secrets():
    """settings.json.env содержит прокси-креды — НЕ должны утечь в вывод."""
    settings = {
        "env": {"HTTP_PROXY": "http://user:SECRETPASS@1.2.3.4:9"},
        "apiKeyHelper": "echo sk-ant-TOPSECRET",
        "model": "claude-opus-5",
        "permissions": {"defaultMode": "bypassPermissions"},
    }
    with patch.object(nc, "read_claude_settings", return_value=settings):
        text = nc.render_config_report()
    assert "SECRETPASS" not in text
    assert "TOPSECRET" not in text
    assert "HTTP_PROXY" not in text
    assert "claude-opus-5" in text  # безопасное — показываем
    assert "bypassPermissions" in text


def test_permissions_report_shows_mode():
    with patch.object(
        nc,
        "read_claude_settings",
        return_value={"permissions": {"defaultMode": "bypassPermissions"}},
    ):
        text = nc.render_permissions_report()
    assert "bypassPermissions" in text


def test_permissions_report_hides_deny_patterns():
    """Список deny-паттернов раскрывает firewall-постуру — не показываем."""
    settings = {
        "permissions": {
            "defaultMode": "bypassPermissions",
            "allow": ["Read(**)"],
            "deny": ["Read(*secret*)", "Bash(cat *.env)", "Read(**/.*token*)"],
        }
    }
    with patch.object(nc, "read_claude_settings", return_value=settings):
        text = nc.render_permissions_report()
    assert "bypassPermissions" in text
    assert "secret" not in text
    assert ".env" not in text
    assert "token" not in text


def test_model_report_shows_current_and_hint():
    with patch.object(nc, "read_claude_settings", return_value={"model": "claude-opus-5"}):
        text = nc.render_model_report()
    assert "Claude Opus 5" in text
    assert "селектор" in text.lower() or "кнопк" in text.lower()


def test_mcp_report_navigates_to_connections():
    text = nc.render_mcp_report()
    assert "одключени" in text  # «Подключения» без учёта регистра первой буквы


def test_dispatch_known_and_unknown():
    with patch.object(nc, "read_claude_settings", return_value={}):
        assert nc.render_native_command("/config") is not None
        assert nc.render_native_command("/permissions") is not None
        assert nc.render_native_command("/model") is not None
        assert nc.render_native_command("/mcp") is not None
    assert nc.render_native_command("/cost") is None  # passthrough, не наш
    assert nc.render_native_command("hello world") is None
    assert nc.render_native_command("") is None


def test_dispatch_with_args_and_case():
    with patch.object(nc, "read_claude_settings", return_value={}):
        assert nc.render_native_command("/CONFIG") is not None  # регистр
        assert nc.render_native_command("/model opus") is not None  # с аргументом


def test_tui_commands_set():
    assert nc.TUI_COMMANDS == {"/config", "/mcp", "/model", "/permissions"}
