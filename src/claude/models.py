"""Single source of truth for Claude model ids/labels used by both the web
``/api/model`` route and the Telegram bot's ``/model`` handler.

Kept provider-neutral (no fastapi/aiogram imports) so any layer can import it
without pulling in a framework. Mirrors the pinned-id rationale that used to
live in ``src/web/routes_model.py``: we write explicit versioned ids into
``~/.claude/settings.json`` so Claude doesn't silently swap "latest".
"""
from __future__ import annotations

# Pinned explicit model ids (не алиасы opus/sonnet/haiku) — чтобы выбор
# детерминированно писал конкретную версию в settings.json.
KNOWN_MODELS: list[dict[str, str]] = [
    {
        "id": "claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
        "hint": "Сбалансированный — рабочая лошадка для большинства задач.",
    },
    {
        "id": "claude-opus-4-8",
        "label": "Claude Opus 4.8",
        "hint": "Самый сильный — для сложных задач, дороже по токенам.",
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "label": "Claude Haiku 4.5",
        "hint": "Быстрый и дешёвый — для лёгких задач и стриминга.",
    },
]

# Default model when settings.json has none yet.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Старые алиасы → pinned id (для отображения "текущей" и текстового /model).
ALIAS_TO_ID: dict[str, str] = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def valid_model_ids() -> set[str]:
    """Allowlist of pinned ids that may be written to settings.json."""
    return {m["id"] for m in KNOWN_MODELS}


def normalize_model_id(raw: str) -> str:
    """Map a stored alias to its pinned id (для подсветки текущей); иначе как есть."""
    return ALIAS_TO_ID.get(raw, raw)


def resolve_model_input(token: str) -> str | None:
    """Текстовый ``/model <token>``: known алиас/id → канонический pinned id.

    Возвращает ``None`` для пустого/с пробелами/слишком длинного/неизвестного —
    строгий allowlist, т.к. значение уходит в общий settings.json.
    """
    t = (token or "").strip()
    if not t or any(ch.isspace() for ch in t) or len(t) > 128:
        return None
    if t in ALIAS_TO_ID:
        return ALIAS_TO_ID[t]
    if t in valid_model_ids():
        return t
    return None


def model_label(model_id: str) -> str:
    """Human label for a pinned id; the id itself when unknown."""
    for m in KNOWN_MODELS:
        if m["id"] == model_id:
            return m["label"]
    return model_id


def model_hint(model_id: str) -> str:
    """One-line hint for a pinned id; empty string when unknown."""
    for m in KNOWN_MODELS:
        if m["id"] == model_id:
            return m["hint"]
    return ""
