"""/api/files — браузер файлов проекта (дерево, чтение, правка, CRUD, поиск).

Один источник правды для файловых операций веб-UI. Все пути проверяются через
is_path_within_root (анти-traversal). Доступ — через resolve_project_access:
чтение при allowed, запись/CRUD только при level == "full".
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.utils.url_safety import is_path_within_root
from src.web.dependencies import get_current_user_factory
from src.web.project_access import resolve_project_access

logger = structlog.get_logger()

MAX_VIEW_BYTES = 1 * 1024 * 1024  # файлы крупнее не отдаём inline (только скачать)
MAX_DIR_ENTRIES = 1000  # потолок записей на один каталог
MAX_SEARCH_HITS = 200
MAX_SEARCH_FILES = 2000
# M-6: суммарный бюджет прочитанных байт на ОДИН content-поиск. Без него
# 2000 файлов × 1 MiB = ~2 ГБ чтения на запрос → CPU/IO DoS. 64 MiB режут
# худший случай, не мешая нормальному поиску.
MAX_SEARCH_TOTAL_BYTES = 64 * 1024 * 1024
_SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", "build"}


class PathError(ValueError):
    """400 — traversal / невалидный путь / неподходящий тип."""


class NotFound(Exception):
    """404 — файла/каталога нет."""


class Conflict(Exception):
    """409 — рассинхрон mtime / дубликат / непустая папка."""


def _safe_candidate(root: Path, rel: str) -> Path:
    """``root/rel`` после resolve, проверенный на traversal.

    PathError, если результат выходит за пределы ``root`` (через ``..`` или
    симлинк наружу). Пустой ``rel`` → сам ``root``. Невалидный путь (напр.
    встроенный null-байт — на POSIX Path.resolve() кидает голый ValueError) →
    тоже PathError, чтобы _call дал чистый 400, а aggregate_artifacts пропустил
    строку, а не валил 500.
    """
    root_resolved = Path(root).resolve()
    try:
        candidate = (root_resolved / rel).resolve()
    except (OSError, ValueError) as exc:
        raise PathError("invalid path") from exc
    if not is_path_within_root(root_resolved, candidate):
        raise PathError("path escapes project root")
    return candidate


def list_dir(root: Path, rel: str = "") -> dict:
    """Прямые дети каталога ``root/rel``: dirs-first, затем по имени.

    Возвращает ``{"truncated": bool, "entries": [Entry, ...]}``; ленивая
    загрузка — только один уровень. Небезопасные симлинки наружу пропускаются.
    """
    root_resolved = Path(root).resolve()
    base = _safe_candidate(root_resolved, rel)
    if not base.is_dir():
        raise NotFound("directory not found")

    children = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    entries: list[dict] = []
    truncated = False
    for child in children:
        if not is_path_within_root(root_resolved, child):
            continue
        # Проверяем лимит ДО добавления: truncated=True только если реально есть
        # ещё валидный ребёнок сверх лимита (ровно MAX_DIR_ENTRIES → не обрезано).
        if len(entries) >= MAX_DIR_ENTRIES:
            truncated = True
            break
        is_dir = child.is_dir()
        try:
            size = None if is_dir else child.stat().st_size
        except OSError:
            size = None
        entries.append(
            {
                "name": child.name,
                "rel": child.relative_to(root_resolved).as_posix(),
                "type": "dir" if is_dir else "file",
                "size_bytes": size,
            }
        )
    return {"truncated": truncated, "entries": entries}


def read_text_file(root: Path, rel: str) -> dict:
    """Содержимое текстового файла + метаданные.

    ``content`` пустой, если файл бинарный (NUL-байт) или больше
    MAX_VIEW_BYTES — такие отдаются только на скачивание.
    """
    root_resolved = Path(root).resolve()
    candidate = _safe_candidate(root_resolved, rel)
    if not candidate.is_file():
        raise NotFound("file not found")
    st = candidate.stat()
    out = {
        "rel": candidate.relative_to(root_resolved).as_posix(),
        "content": "",
        "size_bytes": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "binary": False,
        "too_large": False,
    }
    if st.st_size > MAX_VIEW_BYTES:
        out["too_large"] = True
        return out
    raw = candidate.read_bytes()
    if b"\x00" in raw:
        out["binary"] = True
        return out
    out["content"] = raw.decode("utf-8", errors="replace")
    return out


def write_text_file(root: Path, rel: str, content: str, expected_mtime_ns: int) -> dict:
    """Перезаписать существующий текстовый файл с проверкой mtime.

    Если ``st_mtime_ns`` на диске отличается от ``expected_mtime_ns`` — Conflict
    (кто-то, например Claude, изменил файл после того как клиент его прочитал).
    """
    root_resolved = Path(root).resolve()
    candidate = _safe_candidate(root_resolved, rel)
    if not candidate.is_file():
        raise NotFound("file not found")
    if candidate.stat().st_mtime_ns != expected_mtime_ns:
        raise Conflict("file changed on disk")
    candidate.write_text(content, encoding="utf-8")
    return {"mtime_ns": candidate.stat().st_mtime_ns}


def create_entry(root: Path, rel: str, kind: str) -> dict:
    """Создать пустой файл (``kind="file"``) или каталог (``kind="dir"``).

    Промежуточные каталоги создаются автоматически. Conflict, если путь уже есть.
    """
    if kind not in ("file", "dir"):
        raise PathError("kind must be 'file' or 'dir'")
    root_resolved = Path(root).resolve()
    candidate = _safe_candidate(root_resolved, rel)
    if candidate == root_resolved or not rel.strip():
        raise PathError("invalid target")
    if candidate.exists():
        raise Conflict("entry already exists")
    if kind == "dir":
        candidate.mkdir(parents=True)
    else:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.touch()
    return {
        "name": candidate.name,
        "rel": candidate.relative_to(root_resolved).as_posix(),
        "type": kind,
        "size_bytes": None if kind == "dir" else 0,
    }


def delete_entry(root: Path, rel: str) -> None:
    """Удалить файл или ПУСТУЮ папку. Непустая папка → Conflict (безопасность)."""
    root_resolved = Path(root).resolve()
    candidate = _safe_candidate(root_resolved, rel)
    if candidate == root_resolved:
        raise PathError("cannot delete project root")
    if candidate.is_dir():
        if any(candidate.iterdir()):
            raise Conflict("directory not empty")
        candidate.rmdir()
    elif candidate.is_file():
        candidate.unlink()
    else:
        raise NotFound("entry not found")


def move_entry(root: Path, src: str, dst: str) -> None:
    """Переименовать/переместить. Conflict, если ``dst`` уже существует."""
    root_resolved = Path(root).resolve()
    src_c = _safe_candidate(root_resolved, src)
    dst_c = _safe_candidate(root_resolved, dst)
    if src_c == root_resolved or not dst.strip():
        raise PathError("invalid move")
    # Нельзя переместить каталог в самого себя или в свой подкаталог
    # (Path.rename бросил бы OSError → неинформативный 500).
    if src_c == dst_c or is_path_within_root(src_c, dst_c):
        raise PathError("cannot move into itself or a subdirectory")
    if not src_c.exists():
        raise NotFound("source not found")
    if dst_c.exists():
        raise Conflict("destination already exists")
    dst_c.parent.mkdir(parents=True, exist_ok=True)
    src_c.rename(dst_c)


def search_project(root: Path, q: str, mode: str) -> dict:
    """Поиск по проекту. ``mode="name"`` — substring по rel-пути; ``"content"``
    — подстрока в тексте файлов. Пропускает _SKIP_DIRS, бинарные и большие
    файлы; ограничен MAX_SEARCH_HITS / MAX_SEARCH_FILES.
    """
    root_resolved = Path(root).resolve()
    if not q:
        return {"truncated": False, "hits": []}
    needle = q.lower()
    hits: list[dict] = []
    files_scanned = 0
    bytes_read = 0  # M-6: суммарный бюджет чтения на запрос
    for dirpath, dirnames, filenames in os.walk(root_resolved):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]  # prune
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if not is_path_within_root(root_resolved, fpath):
                continue
            rel = fpath.relative_to(root_resolved).as_posix()
            if mode == "name":
                if needle in rel.lower():
                    hits.append({"rel": rel, "line": None, "preview": None})
                    if len(hits) >= MAX_SEARCH_HITS:
                        return {"truncated": True, "hits": hits}
                continue
            # mode == "content"
            files_scanned += 1
            if files_scanned > MAX_SEARCH_FILES:
                return {"truncated": True, "hits": hits}
            try:
                if fpath.stat().st_size > MAX_VIEW_BYTES:
                    continue
                raw = fpath.read_bytes()
                bytes_read += len(raw)
                if b"\x00" in raw:
                    continue
                text = raw.decode("utf-8", errors="replace")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    hits.append({"rel": rel, "line": i, "preview": line.strip()[:200]})
                    if len(hits) >= MAX_SEARCH_HITS:
                        return {"truncated": True, "hits": hits}
            # M-6: исчерпали бюджет чтения (после учёта хитов этого файла) —
            # дальше не сканируем, помечаем как обрезанный результат.
            if bytes_read > MAX_SEARCH_TOTAL_BYTES:
                return {"truncated": True, "hits": hits}
    return {"truncated": False, "hits": hits}


def aggregate_artifacts(
    root: Path, tool_rows: list[dict], dismissed: set[str]
) -> list[dict]:
    """Свернуть tool_use-записи в дедуплицированный список артефактов.

    ``tool_rows`` — строки вида ``{"tool", "file_path", "created_at"}`` из всех
    tool_use-сообщений. Оставляем только файлы внутри ``root`` (анти-traversal
    через _safe_candidate); дедуп по resolved абсолютному пути; ``action`` =
    "created", если по этому пути был хоть один Write, иначе "edited". На каждый
    артефакт — статус с диска (exists/size). Сортировка: last_ts по убыванию.

    Внимание: выходное поле ``rel`` здесь — это АБСОЛЮТНЫЙ resolved-путь (posix),
    намеренно отличающийся от ``rel`` в эндпойнтах /tree и /content (там — путь
    относительно корня). Этот абсолютный ``rel`` единообразно используется для
    download (sessionFileUrl), delete (/api/files/entry) и dismiss. Поле
    ``display`` — путь относительно корня для показа в UI.
    """
    root_resolved = Path(root).resolve()
    agg: dict[str, dict] = {}
    for row in tool_rows:
        fp = row.get("file_path")
        if not isinstance(fp, str) or not fp:
            continue
        try:
            cand = _safe_candidate(root_resolved, fp)
        except PathError:
            continue  # вне корня проекта — не наш артефакт
        if cand == root_resolved:
            continue
        key = cand.as_posix()
        # Write и Bash-выводы считаем «созданием» (Bash чаще создаёт, чем правит).
        created = str(row.get("tool", "")).lower() in ("write", "bash")
        ts = row.get("created_at") or ""
        entry = agg.get(key)
        if entry is None:
            agg[key] = {
                "rel": key,
                "name": cand.name,
                "display": cand.relative_to(root_resolved).as_posix(),
                "created": created,
                "edits": 1,
                "last_ts": ts,
            }
        else:
            entry["created"] = entry["created"] or created
            entry["edits"] += 1
            if ts > entry["last_ts"]:
                entry["last_ts"] = ts
    out: list[dict] = []
    for e in agg.values():
        exists = False
        is_dir = False
        size_bytes: int | None = None
        try:
            cand = Path(e["rel"])
            if cand.is_file():
                exists = True
                size_bytes = cand.stat().st_size
            elif cand.is_dir():
                # Папка-артефакт (например, копия каталога через `cp -r`).
                # Скачивается как zip; размер — суммарный по файлам внутри.
                exists = True
                is_dir = True
                size_bytes = _dir_size_bytes(cand)
        except OSError:
            exists = False
            size_bytes = None
        out.append(
            {
                "rel": e["rel"],
                "name": e["name"],
                "display": e["display"],
                "action": "created" if e["created"] else "edited",
                "edits": e["edits"],
                "last_ts": e["last_ts"],
                "exists": exists,
                "is_dir": is_dir,
                "size_bytes": size_bytes,
                "dismissed": e["rel"] in dismissed,
            }
        )
    out.sort(key=lambda a: a["last_ts"], reverse=True)
    return out


def _dir_size_bytes(path: Path, max_files: int = 20000) -> int | None:
    """Суммарный размер файлов в каталоге. None — если слишком много файлов
    (защита от обхода гигантских деревьев). Симлинки не разворачиваем."""
    total = 0
    count = 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for fn in files:
                count += 1
                if count > max_files:
                    return None
                fp = Path(root) / fn
                try:
                    if fp.is_symlink():
                        continue
                    total += fp.stat().st_size
                except OSError:
                    continue
    except OSError:
        return None
    return total


def _normalize_artifact_rel(root: Path, rel: str) -> str:
    """Канонический ключ артефакта (resolved абсолютный путь как posix).

    PathError, если путь выходит за пределы ``root`` — так dismiss нельзя
    применить к чужому файлу (через ``..`` или абсолютный путь наружу).
    """
    cand = _safe_candidate(Path(root).resolve(), rel)
    if cand == Path(root).resolve():
        raise PathError("invalid artifact path")
    return cand.as_posix()


class WriteIn(BaseModel):
    content: str
    expected_mtime_ns: int


class CreateIn(BaseModel):
    rel: str
    kind: str  # "file" | "dir"


class MoveIn(BaseModel):
    src: str
    dst: str


class DismissIn(BaseModel):
    rel: str


def make_files_router(
    *,
    jwt_secret: str,
    project_paths_provider: Callable[[], list],
    session_manager=None,
    allowed_user_ids: list[int] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/files", tags=["files"])
    whitelist = set(allowed_user_ids or [])
    get_current_user = get_current_user_factory(jwt_secret, session_manager, whitelist)
    # M-6: ограничиваем число ОДНОВРЕМЕННЫХ дорогих content-поисков по всему
    # процессу — иначе пачка параллельных запросов насыщает threadpool/диск.
    _content_search_sem = asyncio.Semaphore(3)

    async def _access(project_path: str, user: dict, *, need_full: bool):
        # Сессия без проекта («чистый Claude») — файлов проекта нет.
        if not project_path:
            raise HTTPException(status_code=400, detail="no project for this session")
        access = await asyncio.to_thread(
            resolve_project_access,
            project_path,
            user=user,
            project_paths=project_paths_provider(),
            whitelist=whitelist,
            session_manager=session_manager,
        )
        if not access.allowed or access.root is None:
            raise HTTPException(status_code=403, detail="project not allowed")
        if not access.root.is_dir():
            raise HTTPException(status_code=404, detail="project not found")
        if need_full and access.level != "full":
            raise HTTPException(status_code=403, detail="read-only access")
        return access

    async def _call(fn, *args):
        # Единое отображение helper-исключений на HTTP-коды.
        try:
            return await asyncio.to_thread(fn, *args)
        except PathError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Conflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except OSError as exc:
            # Реальная ошибка ФС (нет прав, файл исчез между stat и read, и т.п.) —
            # чистый 500 без утечки трейсбека вместо необработанного исключения.
            logger.warning("files_fs_error", error=str(exc))
            raise HTTPException(status_code=500, detail="filesystem error")

    @router.get("/tree")
    async def get_tree(
        project_path: str = Query(...),
        subdir: str = Query("", alias="dir"),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=False)
        result = await _call(list_dir, access.root, subdir)
        return {"access_level": access.level, **result}

    @router.get("/content")
    async def get_content(
        project_path: str = Query(...),
        rel: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=False)
        return await _call(read_text_file, access.root, rel)

    @router.put("/content")
    async def put_content(
        payload: WriteIn,
        project_path: str = Query(...),
        rel: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=True)
        return await _call(
            write_text_file, access.root, rel, payload.content, payload.expected_mtime_ns
        )

    @router.post("/entry")
    async def post_entry(
        payload: CreateIn,
        project_path: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=True)
        return await _call(create_entry, access.root, payload.rel, payload.kind)

    @router.delete("/entry")
    async def delete_entry_route(
        project_path: str = Query(...),
        rel: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=True)
        await _call(delete_entry, access.root, rel)
        return {"success": True}

    @router.post("/move")
    async def post_move(
        payload: MoveIn,
        project_path: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=True)
        await _call(move_entry, access.root, payload.src, payload.dst)
        return {"success": True}

    @router.get("/search")
    async def get_search(
        project_path: str = Query(...),
        q: str = Query(...),
        mode: str = Query("name"),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=False)
        if mode not in ("name", "content"):
            raise HTTPException(status_code=400, detail="mode must be name or content")
        if mode == "content":
            # M-6: дорогой поиск по содержимому — под семафором конкурентности.
            async with _content_search_sem:
                return await _call(search_project, access.root, q, mode)
        return await _call(search_project, access.root, q, mode)

    @router.get("/artifacts")
    async def get_artifacts(
        project_path: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=False)
        if session_manager is None:
            return {"access_level": access.level, "artifacts": []}
        # M-7: только артефакты из сессий ЭТОГО пользователя — в общем проекте
        # не показываем чужую файловую активность. L-9: метод ограничивает скан.
        rows = await asyncio.to_thread(
            session_manager.list_tool_artifact_rows, int(user["user_id"])
        )
        dismissed = set(
            await asyncio.to_thread(
                session_manager.list_artifact_dismissals,
                int(user["user_id"]),
                str(access.root),
            )
        )
        artifacts = await asyncio.to_thread(
            aggregate_artifacts, access.root, rows, dismissed
        )
        return {"access_level": access.level, "artifacts": artifacts}

    @router.post("/artifacts/dismiss")
    async def post_artifact_dismiss(
        payload: DismissIn,
        project_path: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=False)
        rel = await _call(_normalize_artifact_rel, access.root, payload.rel)
        if session_manager is not None:
            await asyncio.to_thread(
                session_manager.add_artifact_dismissal,
                int(user["user_id"]),
                str(access.root),
                rel,
            )
        return {"success": True}

    @router.delete("/artifacts/dismiss")
    async def delete_artifact_dismiss(
        project_path: str = Query(...),
        rel: str = Query(...),
        user: dict = Depends(get_current_user),
    ) -> dict:
        access = await _access(project_path, user, need_full=False)
        norm = await _call(_normalize_artifact_rel, access.root, rel)
        if session_manager is not None:
            await asyncio.to_thread(
                session_manager.remove_artifact_dismissal,
                int(user["user_id"]),
                str(access.root),
                norm,
            )
        return {"success": True}

    return router
