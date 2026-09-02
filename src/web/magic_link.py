"""One-time magic-link tokens for web login from the Telegram bot.

The bot's ``/weblogin`` command mints a token here; the web endpoint
``POST /api/auth/magic-link`` consumes it. Tokens are single-use and
expire after a TTL (default 15 minutes).

Storage is in-memory: a process restart invalidates every outstanding
link. That's acceptable — tokens are short-lived and easy to re-mint.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


DEFAULT_TTL_SECONDS = 15 * 60
TOKEN_BYTES = 24  # ~32 url-safe chars


@dataclass(frozen=True)
class _Entry:
    user_id: int
    expires_at: float  # monotonic-ish unix timestamp


class MagicLinkStore:
    """In-memory TTL store for one-time login tokens."""

    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._tokens: dict[str, _Entry] = {}

    def create(self, user_id: int, *, now: float | None = None) -> str:
        """Mint a fresh token bound to ``user_id``.

        Lazily evicts any expired entries before insertion so the store
        size stays bounded even if many tokens are minted and abandoned.
        """
        ts = now if now is not None else time.time()
        token = secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            self._evict_expired(ts)
            self._tokens[token] = _Entry(
                user_id=int(user_id),
                expires_at=ts + self.ttl_seconds,
            )
        return token

    def consume(self, token: str, *, now: float | None = None) -> int | None:
        """Validate and pop the token. Returns the user_id on success.

        Returns ``None`` if the token is unknown, expired, or has already
        been used. Constant-time-ish; the in-memory dict lookup itself
        is not constant-time, but token values are unguessable so the
        timing leak is irrelevant.
        """
        if not token:
            return None
        ts = now if now is not None else time.time()
        with self._lock:
            entry = self._tokens.pop(token, None)
            self._evict_expired(ts)
            if entry is None:
                return None
            if entry.expires_at < ts:
                return None
            return entry.user_id

    def _evict_expired(self, now: float) -> None:
        expired = [tok for tok, e in self._tokens.items() if e.expires_at < now]
        for tok in expired:
            self._tokens.pop(tok, None)
