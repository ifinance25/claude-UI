"""Простой in-memory rate-limiter для эндпоинтов входа (защита от брутфорса).

Без внешних зависимостей. Считает неудачные попытки на ключ (IP+username) в
скользящем окне; после порога — временная блокировка с возрастающим временем.
Успешный вход сбрасывает счётчик. Хранилище в памяти — рестарт процесса
обнуляет лимиты (приемлемо: окно короткое).
"""
from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    fails: int = 0
    window_start: float = 0.0
    locked_until: float = 0.0


@dataclass
class LoginRateLimiter:
    """Лимитер неудачных попыток входа.

    max_fails неудач в окне window_seconds → блок на lockout_seconds
    (удваивается при повторных сериях, но не больше max_lockout_seconds).
    """

    max_fails: int = 8
    window_seconds: float = 300.0
    lockout_seconds: float = 300.0
    max_lockout_seconds: float = 3600.0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _now(self) -> float:
        return time.time()

    def retry_after(self, key: str) -> float:
        """Сколько секунд осталось до разблокировки (0 — не заблокирован)."""
        now = self._now()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                return 0.0
            return max(0.0, b.locked_until - now)

    def is_blocked(self, key: str) -> bool:
        return self.retry_after(key) > 0.0

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            b = self._buckets.get(key)
            if b is None or now - b.window_start > self.window_seconds:
                b = _Bucket(fails=0, window_start=now)
                self._buckets[key] = b
            b.fails += 1
            if b.fails >= self.max_fails:
                # Возрастающая блокировка: lockout * (число превышений порога).
                over = b.fails - self.max_fails + 1
                lock = min(self.lockout_seconds * over, self.max_lockout_seconds)
                b.locked_until = now + lock
            # Ленивая чистка протухших бакетов, чтобы словарь не рос вечно.
            if len(self._buckets) > 4096:
                self._evict(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def _evict(self, now: float) -> None:
        stale = [
            k
            for k, b in self._buckets.items()
            if b.locked_until < now and now - b.window_start > self.window_seconds
        ]
        for k in stale:
            self._buckets.pop(k, None)


def _is_trusted_proxy(host: str) -> bool:
    """Доверяем X-Forwarded-For только если соединение пришло с loopback —
    т.е. от локального reverse-proxy (Caddy/nginx на 127.0.0.1), как в нашем
    деплое (app слушает 127.0.0.1, фронт — Caddy на том же хосте)."""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def client_key(request, username: str = "") -> str:
    """Ключ лимита: IP клиента + username.

    Анти-спуфинг: X-Forwarded-For доверяем ТОЛЬКО когда соединение пришло с
    доверенного (loopback) reverse-proxy, и берём ПОСЛЕДНИЙ адрес цепочки —
    его проставляет наш прокси (реальный клиент). Левые элементы XFF клиент
    может подделать (nginx `$proxy_add_x_forwarded_for`/Caddy дописывают
    remote_addr справа), поэтому `split[0]` использовать нельзя — иначе
    атакующий рандомизирует первый элемент и обходит лимит. При прямом
    подключении (прокси не на loopback) XFF игнорируем и берём адрес
    соединения — спуфинг не работает."""
    client_host = (request.client.host if request.client else "") or ""
    xff = request.headers.get("x-forwarded-for", "")
    if xff and _is_trusted_proxy(client_host):
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        ip = parts[-1] if parts else client_host
    else:
        ip = client_host
    return f"{ip}|{username.lower()}"
