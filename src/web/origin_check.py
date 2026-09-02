"""Проверка Origin запросов — общая для WebSocket-handshake (защита от CSWSH)
и REST-мутаций (защита от CSRF).

Браузер всегда проставляет заголовок ``Origin`` на cross-site WS-upgrade и на
cross-site state-changing запросах, и JS не может его подделать. Поэтому:
  - запрос без Origin (curl / не-браузер / TestClient) — пропускаем (это не
    CSRF/CSWSH-вектор: атака требует браузер с куками жертвы);
  - Origin с тем же хостом, что и Host запроса (same-origin за Caddy) — ок;
  - Origin в явном allowlist (public_origin) — ок;
  - loopback-origin (локальная разработка, vite) — ок;
  - всё остальное (чужой сайт) — отклоняем.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def build_allowed_origins(public_origin: str | None) -> set[str]:
    """Множество разрешённых origin'ов из public_origin (схема://host[:port])."""
    allowed: set[str] = set()
    if public_origin:
        parsed = urlsplit(public_origin.strip())
        if parsed.scheme and parsed.netloc:
            allowed.add(f"{parsed.scheme}://{parsed.netloc}")
    return allowed


def is_allowed_origin(
    origin: str | None,
    host: str | None,
    allowed_origins: set[str],
    *,
    allow_loopback: bool = True,
) -> bool:
    """True, если запрос можно считать same-site (или origin отсутствует).

    ``allow_loopback`` (L-6): в проде (публичный origin) пропускать loopback-
    Origin не нужно — это лишняя поверхность. Сервер выставляет False, когда
    public_origin — не loopback. В dev (по умолчанию True) localhost/vite
    по-прежнему разрешены.
    """
    if not origin:
        # Не-браузерный клиент / TestClient. Браузерная CSRF/CSWSH-атака всегда
        # шлёт Origin, поэтому отсутствие заголовка не является вектором.
        return True
    parsed = urlsplit(origin)
    netloc = parsed.netloc
    if not netloc:
        return False
    # Same-origin: Origin совпадает с Host запроса (прод за reverse-proxy).
    if host and netloc == host:
        return True
    normalized = f"{parsed.scheme}://{netloc}" if parsed.scheme else origin
    if normalized.rstrip("/") in {o.rstrip("/") for o in allowed_origins}:
        return True
    # Локальная разработка: vite/браузер на loopback (только если разрешено).
    if allow_loopback:
        hostname = (parsed.hostname or "").lower()
        if hostname in ("localhost", "127.0.0.1", "::1"):
            return True
    return False
