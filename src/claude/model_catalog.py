"""Живой каталог моделей: список из Anthropic ``/v1/models`` + человеческие имена.

Модели выходят часто, а ``KNOWN_MODELS`` в :mod:`src.claude.models` — статичный
список, который правится руками. Пока его не обновили, новая модель приезжала в
интерфейс сырым id (``claude-fable-5-1`` вместо «Claude Fable 5.1»), а иногда не
приезжала вовсе.

Здесь два НЕЗАВИСИМЫХ слоя, и второй работает даже когда первый недоступен:

1. **Живой список.** ``GET /v1/models`` отдаёт ``id`` и ``display_name`` — то
   самое человеческое имя, которое ведёт сам Anthropic. Требует API-ключ; ответ
   кэшируется на :data:`CACHE_TTL_SECONDS`, при любой ошибке (нет ключа, нет
   сети, 401) молча откатываемся к статике — интерфейс не должен падать из-за
   недоступного справочника.
2. **Гуманизатор.** :func:`humanize_model_id` (в ``models.py``, чтобы не было
   кругового импорта) превращает незнакомый id в читаемое имя без всякой сети. Именно
   он закрывает случай «ключа нет, а модель новая».

Сеть намеренно на ``urllib`` из стандартной библиотеки: ради одного GET не стоит
тащить в рантайм-зависимости http-клиент.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import structlog

from src.claude.models import (
    DEFAULT_MODEL,
    KNOWN_MODELS,
    humanize_model_id,
    is_safe_model_id,
)

logger = structlog.get_logger()

API_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
CACHE_TTL_SECONDS = 6 * 60 * 60  # справочник моделей меняется реже, чем раз в день
REQUEST_TIMEOUT_SECONDS = 8.0
MAX_PAGES = 5  # страховка от бесконечной пагинации

# (момент_загрузки, список) — None пока ни одной удачной загрузки не было.
_cache: tuple[float, list[dict[str, str]]] | None = None
_cache_lock = threading.Lock()


def _resolve_api_key() -> str | None:
    """API-ключ для справочника: сначала окружение, затем ``settings.json``.

    Подписочный вход (OAuth) ключа не даёт — это нормальный и ожидаемый случай:
    вызывающий получит ``None``, каталог останется статическим, а имена моделей
    приведёт в порядок гуманизатор.
    """
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    try:
        from src.claude.claude_settings import (
            CLAUDE_SETTINGS_PATH,
            read_claude_settings,
        )

        env = read_claude_settings(CLAUDE_SETTINGS_PATH).get("env") or {}
        if isinstance(env, dict):
            val = str(env.get("ANTHROPIC_API_KEY") or "").strip()
            if val:
                return val
    except Exception:  # noqa: BLE001 — справочник не повод падать
        pass
    return None


def _base_url() -> str:
    return (os.environ.get("ANTHROPIC_BASE_URL") or API_BASE_URL).strip().rstrip("/")


def fetch_remote_models(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> list[dict[str, str]]:
    """``GET /v1/models`` со сквозной пагинацией → ``[{id, label}]``.

    Порядок ответа сохраняем как есть — Anthropic отдаёт новые модели первыми,
    и это ровно тот порядок, который нужен в пикере.
    """
    root = (base_url or _base_url()).rstrip("/")
    out: list[dict[str, str]] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        url = f"{root}/v1/models?limit=100"
        if after:
            url += f"&after_id={urllib.parse.quote(after)}"
        req = urllib.request.Request(
            url,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload: Any = json.load(resp)
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            # Строгая проверка формата: значение уезжает в allowlist, по которому
            # разрешена запись в общий settings.json.
            if not is_safe_model_id(model_id):
                continue
            label = str(item.get("display_name") or "").strip()
            out.append({"id": model_id, "label": label or humanize_model_id(model_id)})
        if not payload.get("has_more"):
            break
        after = str(payload.get("last_id") or "").strip()
        if not after:
            break
    return out


def _hint_for(model_id: str) -> str:
    """Подсказка из статики: точное совпадение, иначе — по семейству модели."""
    for m in KNOWN_MODELS:
        if m["id"] == model_id:
            return m.get("hint", "")
    for m in KNOWN_MODELS:
        family = m["id"].split("-")[1] if "-" in m["id"] else ""
        if family and model_id.startswith(f"claude-{family}-"):
            return m.get("hint", "")
    return ""


def _load_remote() -> list[dict[str, str]]:
    key = _resolve_api_key()
    if not key:
        return []
    try:
        return fetch_remote_models(api_key=key)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        # Нет сети / 401 / мусор в ответе — это не ошибка приложения. Пикер
        # переживёт: ниже вернётся статический список.
        logger.info("model_catalog_fetch_failed", error=str(exc))
        return []


def get_models(*, force_refresh: bool = False) -> list[dict[str, str]]:
    """Каталог для пикера: живой список, иначе статический.

    Живой список — источник правды по СОСТАВУ и ИМЕНАМ (``display_name`` от
    Anthropic); подсказки берём из статики. Модель по умолчанию досыпаем всегда,
    иначе её нельзя было бы выбрать, если справочник её почему-то не вернул.
    """
    global _cache
    with _cache_lock:
        cached = _cache
        fresh = (
            cached is not None
            and not force_refresh
            and (time.monotonic() - cached[0]) < CACHE_TTL_SECONDS
        )
        if fresh and cached is not None:
            remote = cached[1]
        else:
            remote = _load_remote()
            if remote:
                _cache = (time.monotonic(), remote)
            elif cached is not None:
                # Разовый сбой сети не должен ронять уже собранный каталог.
                remote = cached[1]

    if not remote:
        return [dict(m) for m in KNOWN_MODELS]

    out = [
        {"id": m["id"], "label": m["label"], "hint": _hint_for(m["id"])}
        for m in remote
    ]
    known_ids = {m["id"] for m in out}
    for m in KNOWN_MODELS:
        if m["id"] not in known_ids:
            out.append(dict(m))
            known_ids.add(m["id"])
    if DEFAULT_MODEL not in known_ids:
        out.append(
            {
                "id": DEFAULT_MODEL,
                "label": humanize_model_id(DEFAULT_MODEL),
                "hint": _hint_for(DEFAULT_MODEL),
            }
        )
    return out


def catalog_model_ids() -> set[str]:
    """Allowlist id-шников, которые разрешено писать в ``settings.json``."""
    return {m["id"] for m in get_models()}


def label_for(model_id: str) -> str:
    """Человеческое имя из каталога; иначе — гуманизатор."""
    for m in get_models():
        if m["id"] == model_id:
            return m["label"]
    return humanize_model_id(model_id)


def reset_cache() -> None:
    """Сброс кэша — для тестов и ручного обновления справочника."""
    global _cache
    with _cache_lock:
        _cache = None
