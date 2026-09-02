"""Регресс-гард: окно контекста бота — единственный источник (context_hint.CONTEXT_WINDOW).

После консолидации (2026-07-02) display-места бота не должны хардкодить 200000.
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_no_hardcoded_200k_context_limit():
    for rel in (
        "src/utils/streaming.py",
        "src/bot/handlers/messages.py",
        "src/bot/handlers/commands.py",
    ):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "200000" not in text, rel
        assert "200_000" not in text, rel


def test_context_window_is_one_million():
    from src.utils.context_hint import CONTEXT_WINDOW

    assert CONTEXT_WINDOW == 1_000_000
