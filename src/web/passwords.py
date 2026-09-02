"""Хэширование паролей на stdlib (PBKDF2-HMAC-SHA256) — без внешних
зависимостей, кросс-платформенно (важно для Windows-разработки).
Формат: pbkdf2$<iterations>$<salt_hex>$<hash_hex>."""
from __future__ import annotations

import hashlib
import hmac
import os

_ITERATIONS = 240_000
_ALGO = "pbkdf2"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)
