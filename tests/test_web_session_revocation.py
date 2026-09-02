"""Деактивация/удаление локального юзера должны ОТЗЫВАТЬ активную сессию.

Регрессия финального ревью: get_current_user только декодировал JWT и не
перепроверял БД → отключённый/удалённый юзер сохранял доступ до истечения
токена (до 30 дней). Особенно опасно для админа (роль в claim). Фикс:
для ЛОКАЛЬНЫХ аккаунтов (id ≥ 1_000_000_001) проверяем существование +
is_active на каждом запросе. Telegram/whitelist-юзеры (без строки в БД)
не должны ломаться.
"""
from __future__ import annotations

import hashlib
import hmac
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from src.claude.session import SessionManager
from src.config.settings import WebSettings
from src.event_bus.bus import EventBus
from src.web.auth import issue_jwt
from src.web.dependencies import is_account_active
from src.web.passwords import hash_password
from src.web.server import WebServer


@pytest.fixture
def tmp_dir():
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def setup(tmp_dir):
    bus = EventBus()
    sm = SessionManager(storage_path=tmp_dir / "sessions.db")
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="t",
    )
    server = WebServer(
        settings=settings, allowed_user_ids=[100], bot_username="t",
        session_manager=sm, event_bus=bus, bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[tmp_dir],
    )
    try:
        yield server, sm
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()


def _sign(bot_token: str, payload: dict) -> dict:
    secret = hashlib.sha256(bot_token.encode()).digest()
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()) if k != "hash")
    sig = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {**payload, "hash": sig}


async def test_deactivated_local_user_session_revoked(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="u1", password_hash=hash_password("pw123456"), is_admin=False
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/login", json={"username": "u1", "password": "pw123456"}
        )
        assert (await client.get("/api/me")).status_code == 200
        sm.set_user_active(uid, False)  # админ отключил юзера
        r = await client.get("/api/me")
    assert r.status_code == 401  # активная сессия отозвана немедленно


async def test_deleted_local_user_session_revoked(setup):
    server, sm = setup
    uid = sm.create_local_user(
        username="u2", password_hash=hash_password("pw123456"), is_admin=True
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/auth/login", json={"username": "u2", "password": "pw123456"}
        )
        assert (await client.get("/api/me")).status_code == 200
        sm.delete_user(uid)  # админа удалили
        r = await client.get("/api/me")
    assert r.status_code == 401  # роль из claim больше не спасает


async def test_telegram_whitelist_user_without_db_row_still_works(setup):
    server, sm = setup
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = _sign(
            "12345:abc",
            {"id": 100, "first_name": "A", "auth_date": int(time.time())},
        )
        r = await client.post("/api/auth/telegram", json=payload)
        assert r.status_code == 200, r.text
        # У Telegram/whitelist-юзера нет строки в users — доступ сохраняется.
        r = await client.get("/api/me")
    assert r.status_code == 200


# ── BD2: моментальный отзыв Telegram/whitelist-юзеров ───────────────


async def test_is_account_active_whitelist_logic():
    """Telegram-uid вне непустого whitelist → неактивен; пустой/None → без проверки."""
    assert await is_account_active(None, {"user_id": 100}, {100}) is True
    assert await is_account_active(None, {"user_id": 999}, {100}) is False
    assert await is_account_active(None, {"user_id": 999}, set()) is True
    assert await is_account_active(None, {"user_id": 999}, None) is True


async def test_telegram_user_removed_from_whitelist_is_revoked(setup):
    """Снятый из ALLOWED_USER_IDS Telegram-юзер теряет доступ сразу, не по TTL."""
    server, sm = setup  # whitelist = [100]
    token = issue_jwt(
        {"user_id": 777, "username": "x", "is_admin": False},
        secret="x" * 32, ttl_seconds=3600,
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"vels_session": token},
    ) as client:
        r = await client.get("/api/me")
    assert r.status_code == 401  # 777 не в whitelist


async def test_telegram_user_in_whitelist_still_authorized(setup):
    server, sm = setup  # whitelist = [100]
    token = issue_jwt(
        {"user_id": 100, "username": "a", "is_admin": False},
        secret="x" * 32, ttl_seconds=3600,
    )
    transport = ASGITransport(app=server.app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={"vels_session": token},
    ) as client:
        r = await client.get("/api/me")
    assert r.status_code == 200
    # BD4: Telegram-юзер может продолжить в Telegram (owner-check совпадёт).
    assert r.json()["can_continue_in_telegram"] is True


async def test_high_telegram_id_in_whitelist_authorized(tmp_dir):
    """Регресс: Telegram id > LOCAL_USER_ID_MIN, но в whitelist — /api/me 200
    и can_continue_in_telegram=True. Раньше код принимал такой id за локальный
    аккаунт (ищет в БД → нет строки → 401 'account disabled or removed')."""
    big = 1166057082  # реальный современный Telegram id (> 1_000_000_001)
    assert await is_account_active(None, {"user_id": big}, {big}) is True
    sm = SessionManager(storage_path=tmp_dir / "big.db")
    settings = WebSettings(
        enabled=True, host="127.0.0.1", port=0, jwt_secret="x" * 32,
        telegram_bot_username="velsbot",
    )
    server = WebServer(
        settings=settings, allowed_user_ids=[big], bot_username="velsbot",
        session_manager=sm, event_bus=EventBus(), bot_token="12345:abc",
        jwt_secret="x" * 32, project_paths=[tmp_dir],
    )
    try:
        token = issue_jwt(
            {"user_id": big, "username": "", "is_admin": False},
            secret="x" * 32, ttl_seconds=3600,
        )
        transport = ASGITransport(app=server.app)
        async with AsyncClient(
            transport=transport, base_url="http://test",
            cookies={"vels_session": token},
        ) as c:
            r = await c.get("/api/me")
        assert r.status_code == 200, r.text
        assert r.json()["can_continue_in_telegram"] is True
    finally:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
