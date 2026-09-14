"""/api/ws/* routes."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Callable

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from src.claude.bridge import ClaudeImageAttachment
from src.claude.native_commands import render_native_command
from src.event_bus.bus import EventBus
from src.event_bus.events import AgentFinished, UserMessageReceived
from src.utils.url_safety import is_path_within_root
from src.web.auth import decode_jwt
from src.web.dependencies import is_account_active
from src.web.origin_check import is_allowed_origin
from src.web.project_access import resolve_project_access
from src.web.ws_forwarder import WSForwarder

logger = structlog.get_logger()


def no_project_readonly(*, user_id, whitelist, db_is_admin: bool) -> bool:
    """Должна ли сессия БЕЗ проекта запускаться в режиме readonly (C-1).

    «Чистый Claude» без проекта запускается в scratch-каталоге, но Bash/Write
    в нём ничем не ограничены (cwd — не песочница), поэтому full-доступ к нему
    эквивалентен полному RCE. Раньше readonly безусловно выставлялся в False,
    из-за чего ЛЮБОЙ аутентифицированный (в т.ч. локальный readonly-юзер без
    грантов) получал полный Claude — обход всей модели project-access.

    Полный доступ к no-project оставляем только доверенным операторам:
    whitelist (владелец/Telegram-операторы) или админ. Остальным — readonly.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return True
    if uid in set(whitelist or ()):
        return False
    if db_is_admin:
        return False
    return True


def _validate_attachment_source_path(src: str, project_root: Path) -> str | None:
    """Возвращает безопасный относительный путь или None, если traversal.

    ``src`` приходит от клиента и должен указывать на файл внутри
    ``project_root`` (уже резолвнутый каллером — чтобы не делать
    expanduser/resolve на каждое вложение). Любой выход за границы
    (../, абсолютный путь, симлинк) — отбрасываем. Возвращаем
    нормализованный относительный путь со слэшами через '/'.
    """
    if not src:
        return None
    candidate = project_root / src
    if not is_path_within_root(project_root, candidate):
        return None
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    return str(resolved.relative_to(project_root)).replace("\\", "/")


def _resolve_project_root(project_path: str) -> Path | None:
    """Sync helper для asyncio.to_thread — кэшируется один раз на сообщение."""
    if not project_path:
        return None
    try:
        return Path(project_path).expanduser().resolve()
    except (OSError, ValueError):
        return None


def _ensure_scratch_dir(scratch_dir: Path) -> Path:
    """Лениво создаёт scratch-каталог (sync helper для asyncio.to_thread).

    Сессии без проекта запускают Claude здесь. Создаём в момент
    использования, чтобы пустой каталог не плодился на каждом старте сервера.
    Возвращает абсолютный путь.
    """
    path = Path(scratch_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def build_native_command_frames(
    text: str, request_id: str, session_id
) -> list[dict] | None:
    """WS-кадры ответа на нативную (TUI-only) слеш-команду, или None.

    Claude CLI не поддерживает /config /model /mcp /permissions через -p, так
    что веб отвечает сам готовым текстом, НЕ запуская Claude. Имитируем обычный
    обмен user→assistant теми же кадрами, что шлёт WSForwarder: эхо команды,
    agent_started (зажигает индикатор), streaming_update kind=text (пузырь
    ответа), finished (гасит индикатор). ``usage=None`` — чтобы фронт НЕ
    рисовал паразитную плашку «tokens: 0 / $0» (в JS ``{}`` truthy).
    """
    native = render_native_command(text)
    if native is None:
        return None
    return [
        {"type": "user_message", "request_id": request_id, "content": text, "source": "web"},
        {"type": "agent_started", "request_id": request_id},
        {
            "type": "streaming_update",
            "request_id": request_id,
            "kind": "text",
            "content": native,
            "metadata": {},
        },
        {
            "type": "finished",
            "request_id": request_id,
            "session_id": session_id,
            "response_text": native,
            "usage": None,
            "error": None,
            "elapsed_ms": 0,
        },
    ]


def make_ws_router(
    *,
    forwarder: WSForwarder,
    bus: EventBus,
    jwt_secret: str,
    session_manager_factory: Callable,
    project_paths_provider: Callable[[], list] | None = None,
    allowed_user_ids: list[int] | None = None,
    allowed_origins: set[str] | None = None,
    allow_loopback_origin: bool = True,
    scratch_dir: Path | str | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/ws", tags=["ws"])
    whitelist = set(allowed_user_ids or [])
    ws_allowed_origins = allowed_origins or set()
    # M-5: лимит одновременных WS-соединений на пользователя. Иначе один аккаунт
    # открывает сотни каналов (каждый — очередь + потенциальный subprocess
    # Claude на user_message) → исчерпание CPU/RAM/FD. Каждое сообщение в канале
    # уже отменяет предыдущую генерацию этого канала, поэтому кап на соединения
    # ограничивает и число параллельных генераций на юзера.
    ws_conn_counts: dict[int, int] = {}
    MAX_WS_PER_USER = 8
    # Каталог для сессий без проекта. Должен лежать ВНЕ project roots
    # (отдельная песочница) — за это отвечает Settings.get_scratch_dir().
    scratch_base = Path(scratch_dir).expanduser() if scratch_dir else None

    @router.websocket("/sessions/{session_uuid}")
    async def ws_session(websocket: WebSocket, session_uuid: str) -> None:
        # Защита от Cross-Site WebSocket Hijacking: браузер с чужого сайта
        # шлёт WS-upgrade с автоматически прикладываемой кукой жертвы, но с
        # Origin своего сайта. Отклоняем cross-site Origin ДО accept().
        if not is_allowed_origin(
            websocket.headers.get("origin"),
            websocket.headers.get("host"),
            ws_allowed_origins,
            allow_loopback=allow_loopback_origin,
        ):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # Auth via cookie BEFORE accepting the upgrade.
        cookie = websocket.cookies.get("vels_session")
        if not cookie:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        try:
            user = decode_jwt(cookie, secret=jwt_secret)
        except ValueError:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # Pre-handshake ownership check so a non-owner can't even open
        # the channel (no fanout subscription, no queue allocated).
        user_id = int(user["user_id"])
        sm_initial = session_manager_factory()
        if sm_initial is None:
            # Without the session manager we cannot verify ownership —
            # refuse the connection rather than accepting an unverified
            # channel to an arbitrary session_uuid (CR3-2).
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
            return
        # Отзыв сессии в реальном времени — через единый is_account_active
        # (классификация по origin, не по диапазону id: H-2 — иначе Telegram-
        # юзер с id ≥ порога и строкой в users проходил бы хендшейк после
        # снятия из whitelist). whitelisted → пускаем; снятый/отключённый/
        # удалённый → закрываем канал.
        if not await is_account_active(sm_initial, user, whitelist):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        # touch=False makes this lookup read-only (a single SELECT, no
        # write+commit) so the handshake no longer turns into a DB write
        # under the connection lock (CR3-7). Kept synchronous because the
        # ASGI/TestClient handshake must accept() without an intervening
        # thread hop; a touch-free SELECT under the lock is cheap.
        initial = sm_initial.get_session_by_uuid(
            session_uuid, owner_chat_id=user_id, touch=False
        )
        if initial is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # M-5: отклоняем, если у пользователя уже слишком много открытых каналов.
        if ws_conn_counts.get(user_id, 0) >= MAX_WS_PER_USER:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.accept()
        ws_conn_counts[user_id] = ws_conn_counts.get(user_id, 0) + 1
        queue = forwarder.register(session_uuid=session_uuid)

        # Текущая генерация для этого WS-соединения. publish(UserMessageReceived)
        # запускает всю цепочку ответа Claude и блокируется до её конца, поэтому
        # выполняем его фоновой задачей — reader остаётся свободен принимать кадры
        # (в т.ч. stop_generation), пока Claude отвечает.
        current_gen: dict = {"task": None, "request_id": None}

        async def reader() -> None:
            """Client -> server: forward user_message to the bus."""
            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")

                    # Стоп текущей генерации: отменяем фоновую publish-задачу и
                    # шлём finished, чтобы у клиента погас индикатор. Сессия
                    # остаётся пригодной для следующего сообщения. Обрабатываем
                    # ДО проверки на user_message — это отдельный тип кадра.
                    if msg_type == "stop_generation":
                        gen_task = current_gen["task"]
                        if gen_task is not None and not gen_task.done():
                            gen_task.cancel()
                            try:
                                await gen_task
                            except (asyncio.CancelledError, Exception):
                                # Отмена publish-задачи: relay получает
                                # CancelledError внутри своего async-for и
                                # доводит bridge до закрытия (kill подпроцесса).
                                pass
                            # finished оповещает подписчиков (persister/forwarder)
                            # — история и клиент остаются консистентными, а
                            # индикатор у клиента гаснет.
                            await bus.publish(
                                AgentFinished(
                                    request_id=current_gen["request_id"] or "",
                                    chat_id=user_id,
                                    topic_id=initial.topic_id,
                                    session_uuid=session_uuid,
                                    response_text="",
                                )
                            )
                        current_gen["task"] = None
                        current_gen["request_id"] = None
                        continue

                    if msg_type != "user_message":
                        continue

                    sm = session_manager_factory()
                    if sm is None:
                        await websocket.send_json({
                            "type": "error",
                            "error": "session manager unavailable",
                        })
                        # Can't verify ownership without the manager — stop
                        # serving this channel instead of looping (CR3-2).
                        break
                    session = await asyncio.to_thread(
                        sm.get_session_by_uuid,
                        session_uuid,
                        owner_chat_id=user_id,
                        touch=False,
                    )
                    if session is None:
                        await websocket.send_json({
                            "type": "error",
                            "error": "session does not exist or does not belong to you",
                        })
                        continue

                    # Отзыв сессии в реальном времени: если локальный аккаунт
                    # отключили/удалили — или Telegram-юзера сняли из whitelist —
                    # посреди открытого канала, больше не запускаем Claude от
                    # его имени.
                    if not await is_account_active(sm, user, whitelist):
                        await websocket.send_json({
                            "type": "error",
                            "error": "account disabled or removed",
                        })
                        break

                    # Нативные (TUI-only) слеш-команды (/config /model /mcp
                    # /permissions): Claude CLI не поддерживает их через -p →
                    # раньше веб слал их в claude и получал «isn't available».
                    # Отвечаем сами готовым текстом, НЕ запуская генерацию.
                    # Кадры кладём в per-session очередь — writer доставит их
                    # без гонки за сокет с reader.
                    _native_frames = build_native_command_frames(
                        msg.get("text", ""), str(uuid.uuid4()), session.session_id
                    )
                    if _native_frames is not None:
                        # Отменяем незавершённую генерацию этого соединения —
                        # иначе её индикатор/кадры смешаются с нативным ответом
                        # (finished нативного погасил бы индикатор реального
                        # хода). То же делает обычный user_message-путь ниже.
                        _prev = current_gen["task"]
                        if _prev is not None and not _prev.done():
                            _prev.cancel()
                            try:
                                await _prev
                            except (asyncio.CancelledError, Exception):
                                pass
                            current_gen["task"] = None
                            current_gen["request_id"] = None
                        # Нативный ответ ЭФЕМЕРЕН: кадры кладём прямо в очередь,
                        # не публикуя в bus → в историю (persister) не пишется.
                        # Осознанно: это справочный вывод, а не диалог с Claude.
                        for _frame in _native_frames:
                            await queue.put(_frame)
                        continue

                    # Сессия БЕЗ проекта («чистый Claude»): project_path
                    # пустой → не гоняем resolve_project_access (нет проекта →
                    # allow, readonly=False), пропускаем резолв attachment
                    # project_root и запускаем Claude в scratch-каталоге.
                    is_no_project = not session.project_path

                    # Конвертируем входящие attachments в формат,
                    # который умеет принимать bridge. Принимаем только
                    # объекты с непустым source_path — фронту положено
                    # сначала загрузить файл через POST /api/uploads и
                    # уже после положить отданный сервером путь сюда.
                    # Для сессий без проекта attachment-путей нет (некуда
                    # резолвить project_root) — блок пропускаем.
                    raw_attachments = msg.get("attachments") or []
                    attachments: list[ClaudeImageAttachment] = []
                    if (
                        not is_no_project
                        and isinstance(raw_attachments, list)
                        and raw_attachments
                    ):
                        # project_root резолвится ОДИН раз и в отдельном
                        # потоке (Path.resolve блокирует event loop на
                        # медленных FS / симлинках). Сам _validate_...
                        # делает ещё один .resolve() per attachment, но
                        # для уже резолвнутого root это дешёвая операция.
                        project_root = await asyncio.to_thread(
                            _resolve_project_root, session.project_path
                        )
                        if project_root is not None:
                            for a in raw_attachments:
                                if not isinstance(a, dict):
                                    continue
                                src_raw = str(a.get("source_path") or "").strip()
                                if not src_raw:
                                    continue
                                safe_src = _validate_attachment_source_path(
                                    src_raw, project_root
                                )
                                if safe_src is None:
                                    logger.warning(
                                        "ws_attachment_path_rejected",
                                        user_id=user_id,
                                        session_uuid=session_uuid,
                                        source_path=src_raw,
                                    )
                                    continue
                                attachments.append(
                                    ClaudeImageAttachment(
                                        source_path=safe_src,
                                        file_name=str(a.get("file_name") or ""),
                                        mime_type=str(a.get("mime_type") or ""),
                                        base64_data="",
                                        kind=str(a.get("kind") or "image"),
                                    )
                                )

                    # Persister подписан на UserMessageReceived с
                    # priority=50 (см. MessageHistoryPersister.start)
                    # — он успевает записать сообщение в БД до того,
                    # как Claude-bridge запустит свою цепочку нестед-
                    # publish'ей. Поэтому даже если reader будет
                    # отменён сразу после `finished`, user_message уже
                    # лежит в `messages`.
                    # project_path, который реально уйдёт в bus (cwd Claude).
                    # Для сессий с проектом — путь проекта; без проекта —
                    # scratch-каталог (создаётся лениво здесь), либо ""
                    # (legacy-роутер без scratch_dir → bridge стартует в cwd).
                    effective_project_path = session.project_path

                    # Уровень доступа к проекту через единый resolver
                    # (deny-by-default). readonly запрещает Claude менять
                    # файлы. Сопоставление по нормализованному пути —
                    # трейлинг-слэш/под-каталог не обходят грант. Если
                    # доступ отозван после создания сессии — не запускаем
                    # Claude вовсе. Сессия без проекта («чистый Claude»):
                    # полный доступ ТОЛЬКО доверенным операторам (admin /
                    # whitelist), остальным — readonly (C-1), иначе scratch-
                    # сессия обходила бы readonly/project-access (RCE).
                    if is_no_project:
                        db_is_admin = bool(user.get("is_admin"))
                        if not db_is_admin and int(user_id) not in set(whitelist or ()):
                            prow = await asyncio.to_thread(
                                sm.get_user_by_id, int(user_id)
                            )
                            db_is_admin = bool(prow and prow.get("is_admin"))
                        readonly = no_project_readonly(
                            user_id=user_id, whitelist=whitelist, db_is_admin=db_is_admin
                        )
                        if scratch_base is not None:
                            scratch_path = await asyncio.to_thread(
                                _ensure_scratch_dir, scratch_base
                            )
                            effective_project_path = str(scratch_path)
                    elif project_paths_provider is not None:
                        access = await asyncio.to_thread(
                            resolve_project_access,
                            session.project_path,
                            user=user,
                            project_paths=project_paths_provider(),
                            whitelist=whitelist,
                            session_manager=sm,
                        )
                        if not access.allowed:
                            await websocket.send_json({
                                "type": "error",
                                "error": "no access to this project",
                            })
                            continue
                        readonly = access.level == "readonly"
                    else:
                        # Legacy-путь (роутер без project_paths_provider):
                        # сохраняем прежнее поведение на точном сравнении.
                        access_level = await asyncio.to_thread(
                            sm.get_access_level, user_id, session.project_path
                        )
                        readonly = access_level == "readonly"

                    # H-1: confine непривилегированного юзера (не admin/не
                    # whitelist) в корень проекта — PreToolUse-firewall не даст
                    # Claude читать секреты/выходить за проект, даже при FULL
                    # доступе и bypassPermissions. Непривилегированную сессию БЕЗ
                    # проекта confine'им в scratch (иначе Read/Grep/Glob читают
                    # весь диск — CRITICAL). Привилегированные — без confine.
                    confine_root = None
                    if is_no_project:
                        # Непривилегированная (readonly) no-project-сессия: confine в
                        # scratch-каталог, чтобы firewall ограничил Read/Grep/Glob и
                        # закрыл чтение .env / ~/.claude / чужих проектов (CRITICAL).
                        # Привилегированные (admin/whitelist) — readonly=False, без confine.
                        # privileged для no-project == not readonly: no_project_readonly
                        # возвращает readonly=False ровно для whitelist/admin.
                        privileged = not readonly
                        if readonly and effective_project_path:
                            confine_root = effective_project_path
                    else:
                        is_priv = int(user_id) in set(whitelist or ())
                        if not is_priv:
                            if user.get("is_admin"):
                                is_priv = True
                            else:
                                prow = await asyncio.to_thread(
                                    sm.get_user_by_id, int(user_id)
                                )
                                is_priv = bool(prow and prow.get("is_admin"))
                        privileged = is_priv
                        if not is_priv:
                            confine_root = effective_project_path

                    # Новое сообщение отменяет предыдущую незавершённую
                    # генерацию этого соединения (клиент шлёт следующий промпт,
                    # не дождавшись ответа) — иначе осталась бы висящая задача.
                    prev_task = current_gen["task"]
                    if prev_task is not None and not prev_task.done():
                        prev_task.cancel()
                        try:
                            await prev_task
                        except (asyncio.CancelledError, Exception):
                            pass

                    # publish(UserMessageReceived) запускает всю цепочку ответа
                    # Claude и блокируется до конца — выполняем фоновой задачей,
                    # чтобы reader мог принять stop_generation во время генерации.
                    # request_id генерируется ОДИН раз и сохраняется, чтобы стоп
                    # завершил именно эту генерацию (тем же request_id).
                    req_id = str(uuid.uuid4())
                    current_gen["request_id"] = req_id
                    current_gen["task"] = asyncio.create_task(
                        bus.publish(
                            UserMessageReceived(
                                request_id=req_id,
                                chat_id=user_id,
                                user_id=user_id,
                                topic_id=session.topic_id,
                                session_uuid=session.session_uuid,
                                project_path=effective_project_path,
                                project_name=session.project_name,
                                text=msg.get("text", ""),
                                session_id=session.session_id,
                                source="web",
                                attachments=attachments,
                                readonly=readonly,
                                privileged=privileged,
                                confine_root=confine_root,
                            )
                        )
                    )
            except WebSocketDisconnect:
                raise

        async def writer() -> None:
            """Server -> client: drain the per-session queue.

            M-4: периодически (не чаще раза в RECHECK сек, чтобы не бить БД на
            каждый чанк) перепроверяем активность аккаунта. Если оператора
            отключили/сняли из whitelist посреди открытого стрима — закрываем
            соединение (1008), иначе writer продолжал бы доставлять весь поток
            ответа Claude уже отозванному пользователю.
            """
            RECHECK = 5.0
            loop = asyncio.get_event_loop()
            last_check = loop.time()
            try:
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=RECHECK)
                        await websocket.send_json(message)
                    except asyncio.TimeoutError:
                        pass
                    now = loop.time()
                    if now - last_check >= RECHECK:
                        last_check = now
                        sm = session_manager_factory()
                        if sm is not None and not await is_account_active(
                            sm, user, whitelist
                        ):
                            await websocket.close(code=1008)
                            return
            except WebSocketDisconnect:
                raise

        reader_task = asyncio.create_task(reader())
        writer_task = asyncio.create_task(writer())

        try:
            done, pending = await asyncio.wait(
                {reader_task, writer_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # Drain cancelled tasks so their CancelledError/exception is
            # retrieved (avoids "Task exception was never retrieved" and a
            # reader detached mid to_thread/publish) (CR3-15).
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            # Surface unexpected exceptions for logging
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    logger.error("ws_session_task_error", error=str(exc))
        finally:
            # Отменяем незавершённую фоновую генерацию, чтобы при разрыве
            # соединения не осталось висящей publish-задачи (и подпроцесса
            # Claude). Сам bridge закрывает подпроцесс по CancelledError.
            gen_task = current_gen["task"]
            if gen_task is not None and not gen_task.done():
                gen_task.cancel()
                try:
                    await gen_task
                except (asyncio.CancelledError, Exception):
                    pass
            forwarder.unregister(session_uuid=session_uuid, queue=queue)
            # M-5: освобождаем слот соединения пользователя.
            remaining = ws_conn_counts.get(user_id, 1) - 1
            if remaining > 0:
                ws_conn_counts[user_id] = remaining
            else:
                ws_conn_counts.pop(user_id, None)

    return router
