"""Tests for Telegram Login Widget HMAC verification + JWT helpers."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from src.web.auth import decode_jwt, issue_jwt, verify_telegram_login


def _sign_login_payload(bot_token: str, payload: dict) -> dict:
    """Helper: produce a valid signed payload like Telegram Login Widget would."""
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash"
    )
    sig = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


def test_valid_telegram_login_accepted():
    bot_token = "12345:abc"
    payload = _sign_login_payload(
        bot_token,
        {"id": 100, "first_name": "A", "auth_date": int(time.time())},
    )
    user = verify_telegram_login(
        payload,
        bot_token=bot_token,
        allowed_user_ids=[100],
        max_age_seconds=86400,
    )
    assert user["id"] == 100


def test_unknown_user_rejected():
    bot_token = "12345:abc"
    payload = _sign_login_payload(
        bot_token,
        {"id": 999, "first_name": "X", "auth_date": int(time.time())},
    )
    with pytest.raises(PermissionError):
        verify_telegram_login(
            payload,
            bot_token=bot_token,
            allowed_user_ids=[100],
            max_age_seconds=86400,
        )


def test_tampered_hash_rejected():
    bot_token = "12345:abc"
    payload = _sign_login_payload(
        bot_token,
        {"id": 100, "first_name": "A", "auth_date": int(time.time())},
    )
    payload["hash"] = "0" * 64
    with pytest.raises(ValueError):
        verify_telegram_login(
            payload,
            bot_token=bot_token,
            allowed_user_ids=[100],
            max_age_seconds=86400,
        )


def test_expired_payload_rejected():
    bot_token = "12345:abc"
    payload = _sign_login_payload(
        bot_token,
        {"id": 100, "first_name": "A", "auth_date": int(time.time()) - 100000},
    )
    with pytest.raises(ValueError):
        verify_telegram_login(
            payload,
            bot_token=bot_token,
            allowed_user_ids=[100],
            max_age_seconds=86400,
        )


def test_jwt_roundtrip():
    secret = "x" * 32
    token = issue_jwt(
        {"user_id": 100, "username": "alice"},
        secret=secret,
        ttl_seconds=3600,
    )
    payload = decode_jwt(token, secret=secret)
    assert payload["user_id"] == 100
    assert payload["username"] == "alice"


def test_jwt_rejects_wrong_secret():
    token = issue_jwt({"user_id": 100}, secret="a" * 32, ttl_seconds=3600)
    with pytest.raises(ValueError):
        decode_jwt(token, secret="b" * 32)
