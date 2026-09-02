"""Подсказка «контекст почти полон» для бота: чистая логика + per-topic флаг.

У бота нет постоянного индикатора (нет chrome — только сообщения), поэтому
шлём одноразовую подсказку при первом пересечении порога за сессию. Логика
вынесена сюда, чтобы тестироваться без Telegram/стримера.
"""
from __future__ import annotations

CONTEXT_WINDOW = 1_000_000
HINT_THRESHOLD_PCT = 85


def context_fill(usage: dict | None) -> int:
    """Токены, которые Claude держал в окне за ход: input + cache read + cache create.

    Терпит оба формата ключей (SDK cache_*_input_tokens и краткий cache_*_tokens).
    """
    if not isinstance(usage, dict):
        return 0
    inp = usage.get("input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0)) or 0
    cache_create = usage.get(
        "cache_creation_input_tokens", usage.get("cache_creation_tokens", 0)
    ) or 0
    return int(inp) + int(cache_read) + int(cache_create)


def context_percent(real_context: int, window: int = CONTEXT_WINDOW) -> int:
    if window <= 0:
        return 0
    return min(100, round(real_context / window * 100))


def hint_text(pct: int) -> str:
    return (
        f"📈 Контекст почти полон ({pct}%). "
        "Пора начать новую тему или отправь /compact, чтобы сжать историю."
    )


class ContextHintGate:
    """Держит per-topic флаг «подсказка показана». Метод decide решает,
    нужно ли слать подсказку сейчас, и возвращает текст либо None."""

    def __init__(self) -> None:
        self._shown: dict[int, bool] = {}

    def decide(self, topic_id: int, usage: dict | None) -> str | None:
        pct = context_percent(context_fill(usage))
        if pct < HINT_THRESHOLD_PCT:
            # Ниже порога — сбрасываем флаг, чтобы подсказка могла сработать
            # снова позже (после /compact или новой темы).
            self._shown[topic_id] = False
            return None
        if self._shown.get(topic_id):
            return None
        self._shown[topic_id] = True
        return hint_text(pct)
