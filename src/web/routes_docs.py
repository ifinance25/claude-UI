"""/api/docs — просмотр markdown-документации выбранного проекта (read-only).

Перечисляет и отдаёт .md-файлы внутри корня проекта. Все пути проверяются
через is_path_within_root, чтобы исключить traversal за пределы проекта.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.utils.url_safety import is_path_within_root
from src.web.dependencies import get_current_user_factory
from src.web.project_access import resolve_project_access

logger = structlog.get_logger()

# Каталоги, которые не сканируем (шум/тяжесть).
_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build"}
_MAX_FILES = 500

# Статический пользовательский гайд (project-independent). Лежит в репозитории:
# src/web/routes_docs.py → parents[2] == корень репо → docs/USER-GUIDE.md.
_DEFAULT_GUIDE_PATH = Path(__file__).resolve().parents[2] / "docs" / "USER-GUIDE.md"


def list_project_docs(root: Path) -> list[dict[str, object]]:
    """Список .md-файлов проекта: rel-путь + размер. Отсортирован."""
    root = Path(root).resolve()
    out: list[dict[str, object]] = []
    for path in sorted(root.rglob("*.md")):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not is_path_within_root(root, path):
            continue
        out.append(
            {
                "rel": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
            }
        )
        if len(out) >= _MAX_FILES:
            break
    return out


def read_project_doc(root: Path, rel: str) -> str:
    """Содержимое одного .md-файла. ValueError при traversal/не-md."""
    root = Path(root).resolve()
    candidate = (root / rel).resolve()
    if not is_path_within_root(root, candidate):
        raise ValueError("path escapes project root")
    if candidate.suffix.lower() != ".md":
        raise ValueError("only markdown files are allowed")
    if not candidate.is_file():
        raise ValueError("file not found")
    return candidate.read_text(encoding="utf-8", errors="replace")


def make_docs_router(
    *,
    jwt_secret: str,
    project_paths_provider: Callable[[], list],
    session_manager=None,
    allowed_user_ids: list[int] | None = None,
    guide_path: Path | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["docs"])
    whitelist = set(allowed_user_ids or [])
    get_current_user = get_current_user_factory(jwt_secret, session_manager, whitelist)
    guide_file = Path(guide_path) if guide_path is not None else _DEFAULT_GUIDE_PATH

    async def _validate_project(project_path: str, user: dict) -> Path:
        # Единый резолвер доступа (как sessions/ws/model/uploads): containment
        # в настроенные корни + per-user грант, deny-by-default, нормализация
        # пути. Доки read-only — readonly-грант доступ даёт (чтение разрешено).
        access = await asyncio.to_thread(
            resolve_project_access,
            project_path,
            user=user,
            project_paths=project_paths_provider(),
            whitelist=whitelist,
            session_manager=session_manager,
        )
        if not access.allowed or access.root is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="project not allowed",
            )
        if not access.root.is_dir():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
            )
        return access.root

    @router.get("/docs/guide")
    async def get_docs_guide(
        user: dict = Depends(get_current_user),
    ) -> dict[str, str]:
        """Статический пользовательский гайд — один и тот же для всех сессий
        (включая сессии без проекта). От project_path не зависит."""
        try:
            content = await asyncio.to_thread(
                guide_file.read_text, encoding="utf-8", errors="replace"
            )
        except FileNotFoundError:
            logger.error("docs_guide_missing", path=str(guide_file))
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="guide not found",
            )
        except OSError as exc:
            logger.error("docs_guide_read_error", path=str(guide_file), error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to read guide",
            )
        return {"rel": "USER-GUIDE.md", "content": content}

    @router.get("/docs")
    async def get_docs(
        project_path: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> list[dict[str, object]]:
        root = await _validate_project(project_path, user)
        return await asyncio.to_thread(list_project_docs, root)

    @router.get("/docs/content")
    async def get_doc_content(
        project_path: str = Query(...),
        rel: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict[str, str]:
        root = await _validate_project(project_path, user)
        try:
            content = await asyncio.to_thread(read_project_doc, root, rel)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            )
        return {"rel": rel, "content": content}

    return router
