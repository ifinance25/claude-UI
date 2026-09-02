"""Single source of truth for Claude model ids/labels used by both the web
``/api/model`` route and the Telegram bot's ``/model`` handler.

Kept provider-neutral (no fastapi/aiogram imports) so any layer can import it
without pulling in a framework. Mirrors the pinned-id rationale that used to
live in ``src/web/routes_model.py``: we write explicit versioned ids into
``~/.claude/settings.json`` so Claude doesn't silently swap "latest".
"""
from __future__ import annotations

import re

# Pinned explicit model ids (не алиасы opus/sonnet/haiku/fable) — чтобы выбор
# детерминированно писал конкретную версию в settings.json.
# Актуальная линейка Claude 5 (+ Haiku 4.5). Строки id — канонические и
# ПОЛНЫЕ как есть: у моделей Claude 5 нет датовых суффиксов (их нельзя
# дописывать), Haiku пинним явной версией.
# Порядок — новые первыми: так же отдаёт живой каталог, и пикер не приходится
# пересортировывать. Это ОФЛАЙННЫЙ запас: когда есть API-ключ, состав и имена
# приходят из /v1/models (см. model_catalog.py) и правки здесь не нужны.
KNOWN_MODELS: list[dict[str, str]] = [
    {
        "id": "claude-fable-5-1",
        "label": "Claude Fable 5.1",
        "hint": "Самая мощная — для самых сложных задач и длинных агентных "
        "цепочек; дороже всех по токенам.",
    },
    {
        "id": "claude-opus-5",
        "label": "Claude Opus 5",
        "hint": "Сильная — флагман для кода и агентных задач.",
    },
    {
        "id": "claude-sonnet-5",
        "label": "Claude Sonnet 5",
        "hint": "Сбалансированная — рабочая лошадка для большинства задач.",
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "label": "Claude Haiku 4.5",
        "hint": "Быстрая и дешёвая — для лёгких задач и стриминга.",
    },
]

# Default model when settings.json has none yet.
DEFAULT_MODEL = "claude-sonnet-5"

# Короткие алиасы → pinned id (для отображения "текущей" и текстового /model).
ALIAS_TO_ID: dict[str, str] = {
    "fable": "claude-fable-5-1",
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}


# --- Имена и валидация id -------------------------------------------------
# Каталог моделей живой (см. src/claude/model_catalog.py), поэтому id может
# прийти такой, какого в KNOWN_MODELS ещё нет. Две чистые функции ниже
# обслуживают этот случай без всякой сети.

# Снапшотные суффиксы: `-20251001` (дата) и `-v1` (ревизия) — это техника
# версионирования, а не часть имени модели.
_SNAPSHOT_DATE_RE = re.compile(r"-\d{8}$")
_REVISION_RE = re.compile(r"-v\d+$")
_DIGITS_RE = re.compile(r"^\d+$")
# id уезжает в общий ~/.claude/settings.json — формат проверяем строго.
_SAFE_ID_RE = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
# Хвосты-режимы, которые несут смысл и должны остаться в подписи.
_SUFFIX_LABELS = {"fast": "Fast"}


def is_safe_model_id(model_id: str) -> bool:
    """Годится ли строка как id модели для записи в settings.json."""
    v = (model_id or "").strip()
    return bool(v) and len(v) <= 128 and bool(_SAFE_ID_RE.match(v))


def humanize_model_id(model_id: str) -> str:
    """``claude-fable-5-1`` → ``Claude Fable 5.1``; чужой формат — как есть.

    Нужен, когда модель новее нашего справочника и API-ключа для живого
    каталога нет: без этого интерфейс показывал технический id.
    """
    raw = (model_id or "").strip()
    if not raw:
        return ""
    name = _SNAPSHOT_DATE_RE.sub("", _REVISION_RE.sub("", raw))
    parts = [p for p in name.split("-") if p]
    if not parts or parts[0] != "claude" or len(parts) < 2:
        return raw
    families: list[str] = []
    digits: list[str] = []
    suffixes: list[str] = []
    for part in parts[1:]:
        if _DIGITS_RE.match(part):
            digits.append(part)
        elif part in _SUFFIX_LABELS:
            suffixes.append(_SUFFIX_LABELS[part])
        else:
            families.append(part.capitalize())
    out = ["Claude", *families]
    if digits:
        out.append(".".join(digits))
    out.extend(suffixes)
    return " ".join(out)


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
    """Человеческое имя модели; для незнакомого id — гуманизированное.

    Раньше возвращался сырой id, и новая модель показывалась в интерфейсе как
    ``claude-fable-5-1``. Живой каталог (model_catalog.label_for) знает точное
    ``display_name`` от Anthropic; здесь — офлайн-запасной вариант.
    """
    for m in KNOWN_MODELS:
        if m["id"] == model_id:
            return m["label"]
    return humanize_model_id(model_id)


def model_hint(model_id: str) -> str:
    """One-line hint for a pinned id; empty string when unknown."""
    for m in KNOWN_MODELS:
        if m["id"] == model_id:
            return m["hint"]
    return ""
