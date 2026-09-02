"""Веб перехватывает нативные (TUI-only) слеш-команды и отвечает сам,
не запуская Claude. Тестируем чистый построитель WS-кадров.
"""
from __future__ import annotations

from unittest.mock import patch

from src.claude import native_commands as nc
from src.web.routes_ws import build_native_command_frames


def test_frames_for_config():
    with patch.object(nc, "read_claude_settings", return_value={"model": "claude-opus-5"}):
        frames = build_native_command_frames("/config", "rid1", "sess-abc")
    assert frames is not None
    assert [f["type"] for f in frames] == [
        "user_message",
        "agent_started",
        "streaming_update",
        "finished",
    ]
    assert frames[0]["content"] == "/config"  # эхо команды
    assert all(f["request_id"] == "rid1" for f in frames)
    su = frames[2]
    assert su["kind"] == "text" and "claude-opus-5" in su["content"]
    fin = frames[3]
    assert fin["usage"] is None  # нет паразитной плашки usage/стоимости
    assert fin["session_id"] == "sess-abc"
    assert fin["error"] is None


def test_none_for_passthrough_and_plain():
    assert build_native_command_frames("/cost", "r", "s") is None
    assert build_native_command_frames("hello world", "r", "s") is None
    assert build_native_command_frames("", "r", "s") is None


def test_frames_for_model_and_permissions():
    with patch.object(nc, "read_claude_settings", return_value={}):
        assert build_native_command_frames("/model", "r", "s") is not None
        assert build_native_command_frames("/permissions", "r", None) is not None
