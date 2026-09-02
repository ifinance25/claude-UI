"""Telegram Login Widget HMAC verification and JWT issuance.

Algorithm per https://core.telegram.org/widgets/login#checking-authorization:
- secret_key = SHA256(bot_token)
- data_check_string = sorted "key=value" lines joined by "\\n", excluding 'hash'
- expected_hash = HMAC-SHA256(secret_key, data_check_string)
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import jwt as pyjwt


def verify_telegram_login(
    payload: dict[str, Any],
    *,
    bot_token: str,
    allowed_user_ids: list[int],
    max_age_seconds: int = 86400,
) -> dict[str, Any]:
    """Verify a Telegram Login Widget payload.

    Returns the user dict on success.
    Raises ValueError on tamper/expiry, PermissionError on unknown user.
    """
    if "hash" not in payload:
        raise ValueError("missing hash")

    received_hash = payload["hash"]
    data_check = {k: v for k, v in payload.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_check.items()))

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise ValueError("invalid hash")

    auth_date = int(payload.get("auth_date", 0))
    now = time.time()
    # Нижняя граница (протухание) и верхняя (токен «из будущего», L-14): окно
    # симметрично, допускаем небольшой перекос часов (60 с).
    if auth_date <= 0 or now - auth_date > max_age_seconds or auth_date - now > 60:
        raise ValueError("auth_date expired")

    user_id = int(payload["id"])
    if user_id not in allowed_user_ids:
        raise PermissionError("user not in whitelist")

    return {
        "id": user_id,
        "first_name": payload.get("first_name", ""),
        "username": payload.get("username", ""),
        "photo_url": payload.get("photo_url", ""),
    }


def issue_jwt(claims: dict[str, Any], *, secret: str, ttl_seconds: int) -> str:
    """Issue a JWT with iat/exp claims."""
    # Defense-in-depth: never sign with an empty key. PyJWT happily uses
    # secret="" for HS256, so guarding only in WebServer.start() leaves the
    # auth layer forgeable if the app is ever mounted another way (CR3-L2).
    if not secret:
        raise ValueError("jwt secret must not be empty")
    now = int(time.time())
    payload = {**claims, "iat": now, "exp": now + ttl_seconds}
    return pyjwt.encode(payload, secret, algorithm="HS256")


def decode_jwt(token: str, *, secret: str) -> dict[str, Any]:
    """Decode and verify a JWT. Raises ValueError if invalid/expired."""
    # Reject an empty secret outright — otherwise a token forged with an
    # empty HS256 key would verify (CR3-L2).
    if not secret:
        raise ValueError("jwt secret must not be empty")
    try:
        return pyjwt.decode(token, secret, algorithms=["HS256"])
    except pyjwt.PyJWTError as exc:
        raise ValueError(f"invalid jwt: {exc}") from exc
