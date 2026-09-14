"""/api/auth/* and /api/me routes."""
from __future__ import annotations

import asyncio
import hmac as _hmac
import time
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from src.web.auth import issue_jwt, verify_telegram_login
from src.web.dependencies import get_current_user_factory
from src.web.magic_link import MagicLinkStore
from src.web.passwords import hash_password, verify_password
from src.web.rate_limit import LoginRateLimiter, client_key

logger = structlog.get_logger()

# Фиктивный, но валидный PBKDF2-хэш для выравнивания времени ответа /auth/login:
# когда пользователя нет, всё равно прогоняем verify_password против него, чтобы
# по времени нельзя было отличить «нет такого логина» от «неверный пароль»
# (L-12, тайминг-оракул перечисления логинов). Считается один раз при импорте.
_TIMING_DUMMY_HASH = hash_password("vels-timing-equalizer")


def _hmac_compare(a: str, b: str) -> bool:
    return _hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def make_auth_router(
    *,
    bot_token: str,
    bot_username: str = "",
    allowed_user_ids: list[int],
    jwt_secret: str,
    jwt_ttl_days: int,
    cookie_secure: bool,
    dev_bearer_token: str = "",
    dev_login_enabled: bool = True,
    magic_link_store: MagicLinkStore | None = None,
    session_manager=None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["auth"])
    get_current_user = get_current_user_factory(
        jwt_secret, session_manager, set(allowed_user_ids or [])
    )
    # Анти-брутфорс: бакет на (IP+username) — против таргетированного перебора
    # пароля, и отдельный бакет на чистый IP с большим порогом — против
    # password spraying по множеству логинов с одного адреса (L-11).
    login_limiter = LoginRateLimiter()
    ip_limiter = LoginRateLimiter(max_fails=20)

    def _rl_check(request: Request, username: str = "") -> tuple[str, str]:
        ukey = client_key(request, username)
        ikey = client_key(request, "")
        retry = max(login_limiter.retry_after(ukey), ip_limiter.retry_after(ikey))
        if retry > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="too many attempts, try later",
                headers={"Retry-After": str(int(retry) + 1)},
            )
        return ukey, ikey

    def _rl_fail(keys: tuple[str, str]) -> None:
        login_limiter.record_failure(keys[0])
        ip_limiter.record_failure(keys[1])

    def _rl_reset(keys: tuple[str, str]) -> None:
        login_limiter.reset(keys[0])
        ip_limiter.reset(keys[1])

    # Replay-guard для Telegram-login (L-1): один и тот же подписанный payload
    # (id+auth_date+hash) нельзя проиграть повторно в пределах окна свежести —
    # перехваченный из логов/Referer payload не выдаст второй раз новый JWT.
    _tg_seen: dict[str, float] = {}
    _TG_REPLAY_TTL = 300.0  # = окно свежести max_age_seconds telegram-login

    def _tg_check_replay(payload_hash: str) -> bool:
        now = time.time()
        if len(_tg_seen) > 2048:
            for k, exp in list(_tg_seen.items()):
                if exp <= now:
                    _tg_seen.pop(k, None)
        if _tg_seen.get(payload_hash, 0.0) > now:
            return False  # уже использован — реплей
        _tg_seen[payload_hash] = now + _TG_REPLAY_TTL
        return True

    async def _is_admin(user_id: int) -> bool:
        """Роль пользователя из БД (для claims). Для Telegram/magic-link —
        админ, если в users проставлен is_admin."""
        if session_manager is None:
            return False
        row = await asyncio.to_thread(session_manager.get_user_by_id, user_id)
        return bool(row and row.get("is_admin"))

    def _set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key="vels_session",
            value=token,
            max_age=jwt_ttl_days * 86400,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
        )

    @router.post("/auth/login")
    async def password_login(
        payload: dict[str, Any], response: Response, request: Request
    ) -> dict[str, Any]:
        """Локальный вход по логину/паролю (аккаунты заводит админ)."""
        if session_manager is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="login disabled"
            )
        username = payload.get("username", "")
        password = payload.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            raise HTTPException(status_code=400, detail="bad payload")
        rl_keys = _rl_check(request, username)
        user = await asyncio.to_thread(
            session_manager.get_user_by_username, username
        )
        if not user or not user.get("password_hash"):
            # Анти-тайминг (L-12): прогоняем PBKDF2 против фиктивного хэша, чтобы
            # ответ «нет логина» занимал столько же, сколько «неверный пароль».
            verify_password(password, _TIMING_DUMMY_HASH)
            _rl_fail(rl_keys)
            raise HTTPException(status_code=401, detail="invalid credentials")
        if not verify_password(password, user["password_hash"]):
            _rl_fail(rl_keys)
            raise HTTPException(status_code=401, detail="invalid credentials")
        if not user.get("is_active", 1):
            raise HTTPException(status_code=403, detail="account disabled")
        _rl_reset(rl_keys)
        is_admin = bool(user["is_admin"])
        token = issue_jwt(
            {
                "user_id": user["user_id"],
                "username": user["username"],
                "is_admin": is_admin,
                # Снимок версии токена: при смене пароля версия в БД растёт,
                # старый JWT перестаёт проходить is_account_active (H-3).
                "tv": int(user.get("token_version") or 0),
            },
            secret=jwt_secret,
            ttl_seconds=jwt_ttl_days * 86400,
        )
        _set_session_cookie(response, token)
        return {
            "ok": True,
            "user": {
                "id": user["user_id"],
                "username": user["username"],
                "is_admin": is_admin,
            },
        }

    @router.post("/auth/telegram")
    async def telegram_login(
        payload: dict[str, Any], response: Response
    ) -> dict[str, Any]:
        try:
            user = verify_telegram_login(
                payload,
                bot_token=bot_token,
                allowed_user_ids=allowed_user_ids,
                # L-1: окно свежести = минуты (легитимный вход через виджет
                # происходит за секунды). Бьёт окно реплея даже если in-memory
                # replay-guard (_tg_seen) обнулится рестартом сервиса.
                max_age_seconds=300,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            )

        # L-1: подпись валидна, но не повторный ли это payload?
        if not _tg_check_replay(str(payload.get("hash", ""))):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="login payload already used",
            )

        # H-1/M-10: фиксируем строку users (+username), чтобы приглашение этого
        # человека в проект по @username / числовому id резолвилось.
        if session_manager is not None:
            try:
                await asyncio.to_thread(
                    session_manager.record_telegram_user,
                    int(user["id"]),
                    user.get("username"),
                )
            except Exception as exc:  # noqa: BLE001 — вход важнее записи
                logger.debug("record_telegram_user_failed", error=str(exc))

        is_admin = await _is_admin(int(user["id"]))
        token = issue_jwt(
            {"user_id": user["id"], "username": user["username"], "is_admin": is_admin},
            secret=jwt_secret,
            ttl_seconds=jwt_ttl_days * 86400,
        )
        _set_session_cookie(response, token)
        return {"ok": True, "user": {**user, "is_admin": is_admin}}

    @router.post("/auth/logout")
    async def logout(response: Response) -> dict[str, Any]:
        response.set_cookie(
            key="vels_session",
            value="",
            max_age=0,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
        )
        return {"ok": True}

    @router.post("/auth/dev-login")
    async def dev_login(
        payload: dict[str, Any], response: Response, request: Request
    ) -> dict[str, Any]:
        """v1 dev auth path — bearer token from WEB_DEV_BEARER_TOKEN env.

        Disabled (404) when ``dev_bearer_token`` is empty. Otherwise
        compares the submitted token against the env value in constant time
        and impersonates the first whitelisted user.

        NOTE: prefer /api/auth/magic-link (issued by the Telegram bot's
        /weblogin command). Dev-login stays as a fallback for hosts that
        can't reach the bot.
        """
        # H-6: dev-login можно полностью выключить (web.dev_login_enabled=false)
        # — рекомендуется в проде после перехода на magic-link. Пустой токен
        # тоже отключает путь.
        if not dev_bearer_token or not dev_login_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="dev-login disabled"
            )
        rl_keys = _rl_check(request)
        token = payload.get("token", "")
        if not isinstance(token, str) or not _hmac_compare(token, dev_bearer_token):
            _rl_fail(rl_keys)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer"
            )
        _rl_reset(rl_keys)
        if not allowed_user_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="no allowed users configured",
            )
        impersonated_id = allowed_user_ids[0]
        logger.warning(
            "dev_bearer_token_used",
            user_id=impersonated_id,
            hint="prefer /weblogin in the Telegram bot — magic links are single-use",
        )

        is_admin = await _is_admin(int(impersonated_id))
        jwt_token = issue_jwt(
            {"user_id": impersonated_id, "username": "dev", "is_admin": is_admin},
            secret=jwt_secret,
            ttl_seconds=jwt_ttl_days * 86400,
        )
        _set_session_cookie(response, jwt_token)
        return {
            "ok": True,
            "user": {"id": impersonated_id, "username": "dev", "is_admin": is_admin},
        }

    @router.post("/auth/magic-link")
    async def magic_link_login(
        payload: dict[str, Any], response: Response, request: Request
    ) -> dict[str, Any]:
        """One-time magic-link login. Bot's /weblogin issues the token.

        On expired/used/unknown token, returns 401 with
        ``error_code="expired_or_used"`` so the frontend can show a
        friendly "ask the bot for a fresh link" message.
        """
        if magic_link_store is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="magic-link disabled",
            )
        rl_keys = _rl_check(request)
        token = payload.get("token", "")
        user_id: int | None = None
        if isinstance(token, str):
            user_id = magic_link_store.consume(token)
        if user_id is None:
            _rl_fail(rl_keys)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error_code": "expired_or_used",
                    "message": "magic link is expired, already used, or unknown",
                },
            )
        # Fail-closed (L-14): принимаем только id, который СЕЙЧАС в whitelist.
        # Раньше при пустом allowed_user_ids проверка пропускалась (fail-open) —
        # валидный, но «ничей» токен проходил. Теперь без whitelist — отказ.
        if user_id not in set(allowed_user_ids or ()):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="user not in whitelist",
            )
        _rl_reset(rl_keys)

        # H-1/M-10: magic-link не несёт username, но строку users всё равно
        # фиксируем — тогда приглашение этого юзера по числовому id резолвится
        # (username подтянется, когда он напишет боту).
        if session_manager is not None:
            try:
                await asyncio.to_thread(
                    session_manager.record_telegram_user, int(user_id), None
                )
            except Exception as exc:  # noqa: BLE001 — вход важнее записи
                logger.debug("record_telegram_user_failed", error=str(exc))

        is_admin = await _is_admin(int(user_id))
        jwt_token = issue_jwt(
            {"user_id": user_id, "username": "", "is_admin": is_admin},
            secret=jwt_secret,
            ttl_seconds=jwt_ttl_days * 86400,
        )
        _set_session_cookie(response, jwt_token)
        return {"ok": True, "user": {"id": user_id, "username": "", "is_admin": is_admin}}

    @router.get("/me")
    async def me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        # deeplink «Продолжить в Telegram» работает только для Telegram-аутентиф.
        # юзеров (их web user_id == telegram id == from_user.id, owner-check
        # совпадёт). Определяем по членству в whitelist, а не по диапазону id
        # (Telegram id бывают > LOCAL_USER_ID_MIN). У локальных (логин/пароль)
        # owner-check вернёт None → кнопка вела бы в тупик, поэтому скрываем.
        can_continue = bool(allowed_user_ids) and int(user["user_id"]) in allowed_user_ids
        return {
            "id": user["user_id"],
            "username": user.get("username", ""),
            "is_admin": bool(user.get("is_admin", False)),
            "telegram_bot_username": bot_username,
            "can_continue_in_telegram": can_continue,
        }

    return router
