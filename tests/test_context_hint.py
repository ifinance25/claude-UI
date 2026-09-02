from src.utils.context_hint import (
    ContextHintGate,
    context_fill,
    context_percent,
)


def test_context_fill_both_key_formats():
    assert context_fill({"input_tokens": 100, "cache_read_input_tokens": 50,
                         "cache_creation_input_tokens": 10}) == 160
    assert context_fill({"input_tokens": 100, "cache_read_tokens": 50,
                         "cache_creation_tokens": 10}) == 160
    assert context_fill(None) == 0
    assert context_fill({}) == 0


def test_context_percent():
    assert context_percent(500_000) == 50
    assert context_percent(1_000_000) == 100
    assert context_percent(2_000_000) == 100  # clamp
    assert context_percent(0) == 0


def test_gate_fires_once_at_threshold():
    gate = ContextHintGate()
    low = {"input_tokens": 100_000}          # 10%
    high = {"input_tokens": 900_000}         # 90%
    assert gate.decide(1, low) is None
    text = gate.decide(1, high)
    assert text is not None and "90%" in text
    # второй раз на том же топике — молчим
    assert gate.decide(1, high) is None


def test_gate_resets_below_threshold():
    gate = ContextHintGate()
    high = {"input_tokens": 900_000}         # 90%
    low = {"input_tokens": 100_000}          # 10%
    assert gate.decide(1, high) is not None
    assert gate.decide(1, low) is None       # сброс флага
    assert gate.decide(1, high) is not None  # снова может сработать


def test_gate_topics_independent():
    gate = ContextHintGate()
    high = {"input_tokens": 900_000}
    assert gate.decide(1, high) is not None
    assert gate.decide(2, high) is not None  # другой топик — свой флаг


import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.utils.streaming import ResponseStreamer


def _make_state(topic_id=7, chat_id=555):
    # Минимальный стейт: finalize с пустым буфером и без empty-completion
    # доходит до нашего блока, не трогая draft/лог.
    return SimpleNamespace(
        chat_id=chat_id, topic_id=topic_id, response_buffer="",
        _receiving=False, _done_event=asyncio.Event(), _draft_task=None,
        log_message=None, response_message=None, log_buffer="",
    )


def test_finalize_sends_hint_once():
    bot = SimpleNamespace(send_message=AsyncMock())
    streamer = ResponseStreamer(bot)  # __init__(bot, ...) — остальные с дефолтами
    high = {"input_tokens": 900_000, "output_tokens": 10}

    state = _make_state()
    state._done_event.set()
    asyncio.run(streamer.finalize(state, show_token_usage=False, usage=high,
                                  send_empty_completion=False))
    assert bot.send_message.await_count == 1

    # второй ход на том же топике при высоком контексте — молчим
    state2 = _make_state()
    state2._done_event.set()
    asyncio.run(streamer.finalize(state2, show_token_usage=False, usage=high,
                                  send_empty_completion=False))
    assert bot.send_message.await_count == 1
