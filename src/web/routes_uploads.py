"""/api/uploads — приёмка файлов-вложений из web UI.

Сохраняет файл в ``<project_path>/.claude/uploads/<safe-name>`` и
отдаёт фронту ``source_path`` (относительный путь от корня проекта).
Этот же путь дальше летит в WS-сообщении как часть ``attachments`` и
конвертируется на сервере в ``ClaudeImageAttachment``, чтобы Claude
прочитал файл через Read tool — тот же механизм, что для фото из
Telegram-бота.
"""
from __future__ import annotations

import asyncio
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from src.utils.url_safety import is_path_within_root
from src.web.dependencies import get_current_user_factory, require_owned_session
from src.web.project_access import resolve_project_access

logger = structlog.get_logger()

# 25 MiB — комфортный предел для скриншота / страницы документа.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
ALLOWED_MIME_PREFIXES = ("image/",)
ALLOWED_TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "application/json",
    "application/x-python",
    "application/javascript",
    "text/x-python",
}


def _safe_filename(raw: str) -> str:
    """Очищаем имя файла, чтобы не было path-traversal / пробелов."""
    base = Path(raw).name or "upload"
    return SAFE_NAME_RE.sub("_", base)[:120]


def _project_path_is_safe(
    project_root: Path,
    allowed_roots: list[Path],
) -> bool:
    """``project_root`` обязан лежать внутри одного из ``allowed_roots``.

    Защищает от ситуации, когда ``session.project_path`` указывает
    на ``/`` или симлинк наружу — без проверки файлы лягут на любую
    директорию, доступную процессу. Если allowed_roots пуст, разрешаем
    что угодно (обратная совместимость с тестами без конфига).
    """
    if not allowed_roots:
        return True
    # Единая проверка containment — та же, что для WS-вложений и cleanup
    # в bridge (CR3-18).
    return any(
        is_path_within_root(root.expanduser(), project_root) for root in allowed_roots
    )


def make_uploads_router(
    *,
    jwt_secret: str,
    session_manager,
    allowed_project_roots_provider: Callable[[], list[Path]] | None = None,
    allowed_user_ids: list[int] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["uploads"])
    whitelist = set(allowed_user_ids or [])
    get_current_user = get_current_user_factory(jwt_secret, session_manager, whitelist)

    @router.post("/uploads")
    async def upload(
        session_uuid: str = Form(...),
        file: UploadFile = File(...),
        user: dict = Depends(get_current_user),
    ) -> dict[str, Any]:
        # Проверяем владение сессией — без этого можно было бы загружать
        # файлы в чужие проекты. Используем общий dependency, поднимает
        # 404 при unknown UUID / wrong owner.
        session = await require_owned_session(
            session_manager=session_manager,
            session_uuid=session_uuid,
            user=user,
        )
        if not session.project_path:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="session has no project_path; can't store uploads",
            )

        # Авторизация уровня доступа (deny-by-default). Upload — это запись
        # в проект, поэтому read-only запрещён (иначе read-only-юзер обходил
        # бы --disallowedTools Write, кладя файлы в проект). Перепроверяем
        # ТЕКУЩИЙ доступ (а не только владение) — на случай отзыва гранта
        # после создания сессии. Единый источник правды с sessions/ws/model.
        if allowed_project_roots_provider is not None:
            access = resolve_project_access(
                session.project_path,
                user=user,
                project_paths=allowed_project_roots_provider(),
                whitelist=whitelist,
                session_manager=session_manager,
            )
            if not access.allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="project not allowed",
                )
            if access.level == "readonly":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="read-only access: uploads disabled",
                )

        # MIME-фильтр: картинки + ограниченный список текста. Видео,
        # бинарники и т.п. отклоняем, чтобы не разбухала папка проекта.
        mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
        allowed = (
            any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES)
            or mime in ALLOWED_TEXT_MIMES
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"mime '{mime}' not allowed",
            )

        # Читаем ПОТОКОВО с ранним обрывом (L-10): не тянем всё тело в RAM,
        # чтобы проверить размер ПОСЛЕ — иначе многократные большие POST'ы
        # (особенно при прямом доступе/без body-лимита прокси) дают OOM.
        # Обрываемся, как только перевалили за лимит.
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"file > {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB",
                )
            chunks.append(chunk)
        data = b"".join(chunks)

        project_root = Path(session.project_path).expanduser().resolve()
        if not project_root.is_dir():
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="project_path does not exist on disk",
            )

        # session.project_path хоть и идёт из БД, но изначально пришёл от
        # пользователя при create_session — допускаем загрузки только в
        # настроенные оператором корни.
        allowed_roots = (
            allowed_project_roots_provider() if allowed_project_roots_provider else []
        )
        if not _project_path_is_safe(project_root, allowed_roots):
            logger.warning(
                "web_upload_project_path_outside_allowed_roots",
                project_root=str(project_root),
                user_id=user["user_id"],
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="project_path is outside of allowed roots",
            )

        upload_dir = project_root / ".claude" / "uploads"

        safe = _safe_filename(file.filename or "upload")
        # Префикс из uuid — чтобы одинаковые имена не затирали друг друга
        # даже при одновременной загрузке. Семантика: ".claude/uploads/<uuid>_<name>".
        prefix = f"{uuid4().hex[:12]}_"
        dest = upload_dir / f"{prefix}{safe}"
        # Финальная страховка: destination обязан лежать внутри upload_dir
        # (на случай, если safe_filename что-то пропустит) — та же общая
        # проверка containment, что и везде (CR3-18).
        if not is_path_within_root(upload_dir, dest.parent):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid destination path",
            )

        def _persist() -> None:
            upload_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        await asyncio.to_thread(_persist)

        rel = str(dest.relative_to(project_root)).replace("\\", "/")
        logger.info(
            "web_upload_saved",
            session_uuid=session_uuid,
            file=rel,
            bytes=len(data),
            mime=mime,
        )

        kind = "image" if mime.startswith("image/") else "text"
        return {
            "source_path": rel,
            "file_name": safe,
            "mime_type": mime,
            "kind": kind,
            "size_bytes": len(data),
        }

    return router
