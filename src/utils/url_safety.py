"""Shared URL/host/path safety helpers.

Один источник правды для решений вида "это loopback?" / "это публичный
HTTPS-URL?" / "этот путь внутри корня?". Раньше эти проверки были
разбросаны по коду с расходящимися правилами — server.py знал про
``::1``, commands.py нет; а resolve()+is_relative_to() для защиты от
path-traversal был скопирован в routes_ws, bridge и routes_uploads.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

# Все известные loopback-хосты для IPv4 и IPv6.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback_host(host: str) -> bool:
    """Является ли host loopback-адресом (для bind-настроек, cookie_secure).

    Принимает голое имя хоста без схемы. ``[::1]`` (IPv6 в квадратных
    скобках) обрабатывается отдельно — это формат URL, не host'а.
    """
    if not host:
        return False
    h = host.strip().lower().strip("[]")
    return h in LOOPBACK_HOSTS


def is_loopback_url(url: str) -> bool:
    """URL целиком указывает на loopback (включая IPv6 ``[::1]``)?"""
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    return is_loopback_host(host)


def is_public_https_url(url: str) -> bool:
    """URL пригоден для Telegram InlineKeyboardButton: https + не loopback.

    Telegram отвергает ``http://localhost``, ``http://127.0.0.1``,
    ``http://[::1]`` и любые не-https URL в inline-кнопках. Используется
    в ``cmd_weblogin`` чтобы решить, прислать ли кнопку или fallback в
    ``<code>``-блок.
    """
    if not url or not url.lower().startswith("https://"):
        return False
    return not is_loopback_url(url)


def is_path_within_root(root: Path, candidate: Path) -> bool:
    """True если ``candidate`` после resolve лежит внутри ``root`` (или равен).

    Единая защита от path-traversal для всех мест, принимающих пути от
    клиента (WS-вложения, очистка вложений в bridge, приёмка uploads).
    ``resolve()`` следует симлинкам, поэтому выход за пределы через
    ``..`` или симлинк наружу отсекается. Любая ошибка резолва
    трактуется как "небезопасно" → False.
    """
    try:
        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()
    except (OSError, ValueError):
        return False
    return (
        candidate_resolved == root_resolved
        or candidate_resolved.is_relative_to(root_resolved)
    )
