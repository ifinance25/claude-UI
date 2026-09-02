"""Создание первого администратора при старте, если его ещё нет."""
from __future__ import annotations

import structlog

from src.web.passwords import hash_password

logger = structlog.get_logger()


def ensure_admin_user(session_manager, *, login: str, password: str) -> None:
    """Заводит админа login/password, если пользователя с таким логином нет.

    Идемпотентно: существующего пользователя НЕ перетирает (пароль из .env
    не должен молча сбрасывать сменённый в админке)."""
    if not login or not password:
        return
    existing = session_manager.get_user_by_username(login)
    if existing is not None:
        return
    session_manager.create_local_user(
        username=login, password_hash=hash_password(password), is_admin=True
    )
    logger.info("admin_bootstrapped", username=login)
