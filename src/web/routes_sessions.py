"""/api/sessions routes."""
from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import os
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from src.utils.url_safety import is_path_within_root
from src.web.dependencies import (
    can_manage_members,
    get_current_user_factory,
    require_owned_session,
)
from src.web.models import MessageOut, SessionCreateIn, SessionOut, SessionPatchIn
from src.web.project_access import resolve_project_access

# Пределы упаковки папки в zip (защита от OOM/обхода гигантских деревьев).
_ZIP_MAX_BYTES = 200 * 1024 * 1024
_ZIP_MAX_FILES = 20000


def resolve_session_file(project_root: Path, rel: str) -> Path:
    """Абсолютный путь к файлу внутри проекта. ValueError при traversal/отсутствии."""
    root = Path(project_root).resolve()
    candidate = (root / rel).resolve()
    if not is_path_within_root(root, candidate):
        raise ValueError("path escapes project root")
    if not candidate.is_file():
        raise ValueError("file not found")
    return candidate


def resolve_session_entry(project_root: Path, rel: str) -> Path:
    """Путь к файлу ИЛИ папке внутри проекта (для скачивания папки zip'ом)."""
    root = Path(project_root).resolve()
    candidate = (root / rel).resolve()
    if not is_path_within_root(root, candidate):
        raise ValueError("path escapes project root")
    if not candidate.exists():
        raise ValueError("not found")
    return candidate


def _zip_dir_bytes(dir_path: Path) -> bytes:
    """Упаковать каталог в zip (в памяти). Симлинки пропускаем; превышение
    лимитов → ValueError. arcname — относительно родителя каталога."""
    buf = io.BytesIO()
    total = 0
    count = 0
    base = dir_path.parent
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dir_path, followlinks=False):
            # Не заходим в симлинк-каталоги (защита от выхода за пределы).
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for fn in files:
                fp = Path(root) / fn
                if fp.is_symlink():
                    continue
                count += 1
                if count > _ZIP_MAX_FILES:
                    raise ValueError("too many files")
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
                if total > _ZIP_MAX_BYTES:
                    raise ValueError("directory too large")
                try:
                    zf.write(fp, arcname=fp.relative_to(base).as_posix())
                except OSError:
                    continue
    return buf.getvalue()


def make_sessions_router(
    *,
    jwt_secret: str,
    session_manager,
    project_paths_provider: Callable[[], list] | None = None,
    allowed_user_ids: list[int] | None = None,
    running_tracker=None,
) -> APIRouter:
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])
    whitelist = set(allowed_user_ids or [])
    get_current_user = get_current_user_factory(jwt_secret, session_manager, whitelist)

    async def _to_out(session, user: dict[str, Any]) -> SessionOut:
        # is_running берём из трекера, если он подключён (web-сервер); без
        # него (старые тесты, иные каналы) — False, чтобы контракт не ломался.
        is_running = (
            running_tracker.is_running(session.session_uuid)
            if running_tracker is not None
            else False
        )
        # L-8: сессии без проекта не имеют участников — считать нечего.
        # Иначе вызываем ТОТ ЖЕ предикат, что и require_project_manage, так
        # кнопка «Участники» на фронте не рассинхронится с реальным /members.
        can_manage = (
            await can_manage_members(
                session_manager=session_manager,
                user=user,
                project_id=session.project_id,
                project_path=session.project_path,
            )
            if session.project_id is not None
            else False
        )
        return SessionOut(
            topic_id=session.topic_id,
            session_uuid=session.session_uuid,
            project_path=session.project_path,
            project_name=session.project_name,
            project_id=session.project_id,
            session_id=session.session_id,
            status=str(session.status.value if hasattr(session.status, "value") else session.status),
            message_count=session.message_count,
            total_cost_usd=session.total_cost_usd,
            created_at=session.created_at.isoformat(),
            last_activity=session.last_activity.isoformat(),
            notes=session.notes,
            is_running=is_running,
            can_manage_members=can_manage,
        )

    async def _require_owned_session(session_uuid: str, user: dict):
        """Thin wrapper that binds the shared dependency to this router's session_manager."""
        return await require_owned_session(
            session_manager=session_manager,
            session_uuid=session_uuid,
            user=user,
        )

    @router.get("", response_model=list[SessionOut])
    async def list_sessions(
        q: str = "",
        user: dict = Depends(get_current_user),
    ) -> list[SessionOut]:
        """List the current user's sessions, optionally filtered by `q`.

        Scoped by ``chat_id`` so callers only see their own sessions.
        """
        sessions = await asyncio.to_thread(
            session_manager.search_sessions,
            q,
            owner_chat_id=int(user["user_id"]),
        )
        return [await _to_out(s, user) for s in sessions]

    @router.post(
        "", status_code=status.HTTP_201_CREATED, response_model=SessionOut
    )
    async def create_session_route(
        payload: SessionCreateIn,
        user: dict = Depends(get_current_user),
    ) -> SessionOut:
        """Allocate the next negative topic_id and create a web-origin session.

        Web-originated sessions live in the negative topic_id range so they
        cannot collide with Telegram thread IDs (always positive). The
        public identifier (returned in `session_uuid`) is a generated UUID
        — all client-side routing should use it instead of topic_id.
        """
        # Сессия БЕЗ проекта («чистый Claude»): project_path null/пустой →
        # пропускаем lookup и access-check, храним project_path="" (канон —
        # пустая строка, как и COALESCE в session.py; SessionOut.project_path
        # остаётся str, фронт/groupByDate не спотыкаются на None). Claude для
        # таких сессий запускается в scratch-каталоге (см. routes_ws).
        if not payload.project_path:
            project_path = ""
            project_name = ""
        else:
            # Авторизация project_path ДО создания: deny-by-default. Без неё
            # любой аутентифицированный (в т.ч. read-only) юзер мог создать
            # сессию на произвольном пути сервера → full-доступ к Claude (RCE)
            # и чтение любого файла через /file. Единый источник правды
            # с /api/projects, /api/docs и WS (resolve_project_access).
            project_path = payload.project_path
            if project_paths_provider is not None:
                access = resolve_project_access(
                    project_path,
                    user=user,
                    project_paths=project_paths_provider(),
                    whitelist=whitelist,
                    session_manager=session_manager,
                )
                if not access.allowed:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="project not allowed",
                    )
            project_name = payload.project_name or Path(project_path).name
        # topic_id allocation + INSERT happen atomically inside
        # create_web_session (single connection lock) so two concurrent
        # POSTs can't collide on the same negative id and silently
        # overwrite each other's owner (CR3-1).
        session = await asyncio.to_thread(
            session_manager.create_web_session,
            project_path,
            project_name,
            int(user["user_id"]),
        )
        return await _to_out(session, user)

    @router.delete("/{session_uuid}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_session(
        session_uuid: str,
        user: dict = Depends(get_current_user),
    ) -> Response:
        session = await _require_owned_session(session_uuid, user)
        await asyncio.to_thread(session_manager.close_session, session.topic_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.patch("/{session_uuid}", response_model=SessionOut)
    async def patch_session(
        session_uuid: str,
        payload: SessionPatchIn,
        user: dict = Depends(get_current_user),
    ) -> SessionOut:
        """Update mutable session fields. Currently only `notes`.

        Owner check enforced via `_require_owned_session` — unknown UUID
        and "not yours" both surface as 404.
        """
        session = await _require_owned_session(session_uuid, user)
        if payload.notes is not None:
            ok = await asyncio.to_thread(
                session_manager.set_session_notes,
                session_uuid,
                payload.notes,
                owner_chat_id=int(user["user_id"]),
            )
            # Если запись не прошла (гонка с удалением сессии, неверный
            # owner) — отвечаем 404, а не успехом с эхом payload.
            if not ok:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="session not found",
                )
            # Reflect persisted value in the response without a second SELECT.
            session.notes = payload.notes
        return await _to_out(session, user)

    @router.get("/{session_uuid}/messages", response_model=list[MessageOut])
    async def get_messages(
        session_uuid: str,
        since: int = 0,
        limit: int = 1000,
        user: dict = Depends(get_current_user),
    ) -> list[MessageOut]:
        session = await _require_owned_session(session_uuid, user)
        rows = await asyncio.to_thread(
            session_manager.get_messages_for_topic,
            session.topic_id,
            since_event_id=since,
            limit=limit,
        )

        out: list[MessageOut] = []
        for r in rows:
            meta_raw = r[6]
            metadata: dict[str, Any] | None = None
            if meta_raw:
                try:
                    metadata = json.loads(meta_raw)
                except json.JSONDecodeError:
                    metadata = None
            out.append(
                MessageOut(
                    event_id=r[0],
                    topic_id=r[1],
                    request_id=r[2],
                    type=r[3],
                    kind=r[4],
                    content=r[5] or "",
                    metadata=metadata,
                    created_at=r[7],
                )
            )
        return out

    @router.get("/{session_uuid}/file")
    async def download_session_file(
        session_uuid: str,
        path: str = Query(...),
        inline: int = Query(0),
        user: dict = Depends(get_current_user),
    ) -> FileResponse:
        session = await _require_owned_session(session_uuid, user)
        # Сессия БЕЗ проекта («чистый Claude») не имеет скачиваемых файлов
        # проекта — отказываем явно (403). Не полагаемся на то, что
        # resolve_project_access("") случайно вернёт 403 из-за того, что
        # серверный cwd лежит вне project roots: делаем безопасность явной,
        # а не побочной.
        if not session.project_path:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="no project files for a no-project session",
            )
        # Перепроверяем ТЕКУЩИЙ доступ к проекту, а не только владение
        # сессией: грант мог быть отозван после её создания.
        if project_paths_provider is not None:
            access = resolve_project_access(
                session.project_path,
                user=user,
                project_paths=project_paths_provider(),
                whitelist=whitelist,
                session_manager=session_manager,
            )
            if not access.allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="project not allowed",
                )
        try:
            resolved = await asyncio.to_thread(
                resolve_session_entry, Path(session.project_path), path
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        # Корень проекта целиком в zip не отдаём (path="" → root): защита от
        # случайной упаковки всего проекта одним запросом.
        if resolved == Path(session.project_path).resolve():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="нельзя скачать корень проекта",
            )
        # Папка (например, копия каталога) — отдаём zip'ом.
        if resolved.is_dir():
            try:
                data = await asyncio.to_thread(_zip_dir_bytes, resolved)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"папку нельзя упаковать: {exc}",
                )
            # Имя папки может быть НЕ-ASCII (кириллица) — заголовок HTTP только
            # latin-1, поэтому RFC 5987: ascii-фолбэк + filename* в UTF-8,
            # иначе Response падал с UnicodeEncodeError (500).
            zip_name = f"{resolved.name}.zip"
            ascii_fallback = zip_name.encode("ascii", "ignore").decode() or "download.zip"
            disposition = (
                f'attachment; filename="{ascii_fallback}"; '
                f"filename*=UTF-8''{quote(zip_name)}"
            )
            return Response(
                content=data,
                media_type="application/zip",
                headers={"Content-Disposition": disposition},
            )
        # inline=1 — отдать для ПРОСМОТРА в браузере (картинка/PDF в <img>/<iframe>),
        # а не скачиванием. Безопасный список типов: только image/* и PDF; всё
        # прочее (html/svg-как-документ, исполняемое) — всегда attachment, чтобы
        # ничего не выполнилось в нашем origin. docx/xlsx/csv/html фронт читает
        # через fetch и рендерит сам (в т.ч. в sandbox-iframe), им inline не нужен.
        if inline:
            guessed, _ = mimetypes.guess_type(resolved.name)
            # SVG исключаем намеренно: image/svg+xml при ПРЯМОМ открытии inline-URL
            # (top-level навигация) исполняет встроенные скрипты в нашем origin —
            # XSS. Растровые картинки и PDF безопасны. SVG фронт рендерит в
            # sandbox-iframe по содержимому.
            inline_ok = bool(guessed) and (
                (guessed.startswith("image/") and guessed != "image/svg+xml")
                or guessed == "application/pdf"
            )
            if inline_ok:
                return FileResponse(
                    resolved,
                    media_type=guessed,
                    content_disposition_type="inline",
                    headers={"X-Content-Type-Options": "nosniff"},
                )
        return FileResponse(
            resolved,
            filename=resolved.name,
            media_type="application/octet-stream",
        )

    return router
