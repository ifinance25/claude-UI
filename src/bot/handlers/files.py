"""File upload handlers."""
from __future__ import annotations

import base64
import asyncio
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from aiogram import Bot, Router
from aiogram.types import Message

import structlog

from src.apikeys.policy import NeedsApiKeyError, resolve_session_auth
from src.bot import onboarding
from src.bot.keyboards import create_project_keyboard
from src.bot.permissions import is_bot_privileged
from src.claude import ClaudeBridge, ClaudeEventType, ClaudeImageAttachment, SessionManager
from src.config import Settings
from src.event_bus import AgentFinished, AgentStreamingUpdate, EventBus, UserMessageReceived
from src.utils import ResponseStreamer
from src.utils.url_safety import is_path_within_root

logger = structlog.get_logger()

router = Router(name="files")

# Имя файла из Telegram (message.document.file_name / message.audio.file_name)
# контролируется отправителем. Без очистки `project_path / ".telegram-uploads"
# / file_name` с именем вида "../../../root/.ssh/authorized_keys" вырвался бы
# за каталог загрузок (запись/перезапись произвольного файла от имени сервиса).
# Та же логика, что у web-загрузок (routes_uploads._safe_filename).
_SAFE_UPLOAD_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_upload_name(raw: str, fallback: str = "upload") -> str:
    """Возвращает безопасное базовое имя файла без path-traversal."""
    base = Path(raw or "").name  # срезает любые директории и ../
    cleaned = _SAFE_UPLOAD_NAME_RE.sub("_", base)[:120]
    if cleaned in ("", ".", ".."):
        return fallback
    return cleaned


# ---------------------------------------------------------------------------
# Shared helpers to reduce duplication between file/image upload handlers
# ---------------------------------------------------------------------------


async def _get_upload_context(message: Message):
    """Validate topic and session. Return context tuple or None.

    Returns (settings, session_manager, claude_bridge, bot, session) on
    success.  Sends an error reply and returns ``None`` when validation fails.
    """
    settings: Settings = router.settings  # type: ignore
    session_manager: SessionManager = router.session_manager  # type: ignore
    claude_bridge: ClaudeBridge = router.claude_bridge  # type: ignore
    bot: Bot = router.bot  # type: ignore

    topic_id = message.message_thread_id

    is_authed = await claude_bridge.check_auth()
    if not is_authed:
        await message.answer(onboarding.claude_error_message("auth", "Claude Code auth check failed"), parse_mode="HTML")
        return None

    if not topic_id:
        await message.answer("Загружайте файлы внутри топика AI-Panel после выбора проекта.")
        return None

    session = await session_manager.async_get_session(topic_id)

    if not session:
        projects = settings.get_project_paths()
        if not projects:
            await message.answer(
                onboarding.no_projects_message(settings.get_projects_directory()),
                parse_mode="HTML",
            )
            return None
        await message.answer(
            onboarding.new_session_prompt_message(auto_created=False),
            reply_markup=create_project_keyboard(projects),
            parse_mode="HTML",
        )
        return None

    project_path = Path(session.project_path).expanduser()
    if not project_path.exists():
        await message.answer(
            onboarding.project_missing_message(str(project_path)),
            parse_mode="HTML",
        )
        return None

    return settings, session_manager, claude_bridge, bot, session


def _extract_reply_context(message: Message) -> tuple[str, str]:
    """Return (reply_context, caption) extracted from *message*.

    ``reply_context`` is the text of the message being replied to (or ``""``).
    ``caption`` is the message caption (or ``""``).
    """
    reply = getattr(message, "reply_to_message", None)
    reply_text = ""
    if reply:
        reply_text = getattr(reply, "text", None) or getattr(reply, "caption", None) or ""

    caption = getattr(message, "caption", None) or ""
    return reply_text, caption

_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


class WhisperCLIUnavailableError(RuntimeError):
    """Raised when the local whisper CLI is not installed."""


def setup_file_handlers(
    dp_router: Router,
    settings: Settings,
    session_manager: SessionManager,
    claude_bridge: ClaudeBridge,
    streamer: ResponseStreamer,
    bot: Bot,
    event_bus: EventBus | None = None,
    api_key_store=None,
) -> None:
    """Register file handlers with the router."""
    router.settings = settings  # type: ignore
    router.session_manager = session_manager  # type: ignore
    router.claude_bridge = claude_bridge  # type: ignore
    router.streamer = streamer  # type: ignore
    router.bot = bot  # type: ignore
    router.event_bus = event_bus  # type: ignore
    # SP2 per-user Anthropic keys. None when CONNECTIONS_SECRET_KEY is unset,
    # which keeps the auth gate dormant (every session uses owner creds). Only
    # consulted on the direct-bridge fallback path (event_bus unset); in
    # production the relay applies the gate.
    router.api_key_store = api_key_store  # type: ignore

    dp_router.include_router(router)


@router.message(lambda m: m.document is not None)
async def handle_document(message: Message) -> None:
    """Handle document uploads."""
    if not message.document:
        return

    logger.info(
        "📎 document_upload",
        file_name=message.document.file_name,
        file_size=message.document.file_size,
        user=message.from_user.username if message.from_user else "unknown",
        topic_id=message.message_thread_id,
    )

    if _is_image_document(message.document):
        await _handle_image_upload(
            message,
            file_id=message.document.file_id,
            file_name=message.document.file_name or "image.png",
            mime_type=message.document.mime_type or "image/*",
        )
        return

    await _handle_file_upload(
        message,
        file_id=message.document.file_id,
        file_name=message.document.file_name or "document",
    )


@router.message(lambda m: m.photo is not None)
async def handle_photo(message: Message) -> None:
    """Handle photo uploads."""
    if not message.photo:
        return

    # Get the largest photo
    photo = message.photo[-1]

    bot: Bot = router.bot  # type: ignore
    await bot.send_chat_action(
        message.chat.id, action="upload_photo",
        message_thread_id=message.message_thread_id,
    )

    logger.info(
        "📷 photo_upload",
        file_size=photo.file_size,
        user=message.from_user.username if message.from_user else "unknown",
        topic_id=message.message_thread_id,
    )

    await _handle_image_upload(
        message,
        file_id=photo.file_id,
        file_name="screenshot.jpg",
        mime_type="image/jpeg",
    )


@router.message(lambda m: m.voice is not None)
async def handle_voice(message: Message) -> None:
    """Handle voice messages by transcribing them with local whisper."""
    await _handle_voice_or_audio(
        message,
        file_id=message.voice.file_id,
        file_size=message.voice.file_size,
        file_name="voice.ogg",
        source="voice",
    )


@router.message(lambda m: m.audio is not None)
async def handle_audio(message: Message) -> None:
    """Handle audio file uploads by transcribing them with local whisper."""
    await _handle_voice_or_audio(
        message,
        file_id=message.audio.file_id,
        file_size=message.audio.file_size,
        file_name=message.audio.file_name or "audio.ogg",
        source="audio",
    )


async def _handle_voice_or_audio(
    message: Message,
    *,
    file_id: str,
    file_size: int | None,
    file_name: str,
    source: str,
) -> None:
    """Common handler for voice messages and audio files."""
    try:
        logger.info(
            "🎙️ voice_message",
            file_size=file_size,
            source=source,
            user=message.from_user.username if message.from_user else "unknown",
            topic_id=message.message_thread_id,
        )

        bot: Bot = router.bot  # type: ignore

        try:
            await bot.send_chat_action(
                message.chat.id,
                action="typing",
                message_thread_id=message.message_thread_id,
            )
        except Exception:
            pass

        try:
            with tempfile.TemporaryDirectory(prefix="telegram-voice-") as tmp_dir:
                temp_dir = Path(tmp_dir)
                voice_path = await _download_to_temp_file(
                    bot=bot,
                    file_id=file_id,
                    file_name=file_name,
                    destination_dir=temp_dir,
                )
                recognized_text = await _transcribe_voice_message(voice_path)
        except WhisperCLIUnavailableError:
            await message.answer(
                "Whisper CLI не найден на сервере.\n"
                "Установите: <code>pip install openai-whisper</code>",
                parse_mode="HTML",
            )
            return
        except RuntimeError as exc:
            await message.answer(f"Не удалось распознать голосовое сообщение: {exc}")
            return
        except Exception as exc:
            logger.error("voice_transcription_failed", error=str(exc))
            await message.answer(f"Ошибка при обработке голосового сообщения: {exc}")
            return

        logger.info(
            "voice_transcribed",
            text_preview=recognized_text[:80],
            text_length=len(recognized_text),
        )

        prompt = _build_voice_prompt(message, recognized_text)
        from src.bot.handlers.messages import process_incoming_text

        await process_incoming_text(message, prompt)

    except Exception as exc:
        logger.error("voice_handler_error", error=str(exc), source=source)
        try:
            await message.answer(f"Ошибка обработки голосового сообщения: {exc}")
        except Exception:
            pass


async def _handle_file_upload(
    message: Message,
    file_id: str,
    file_name: str,
) -> None:
    """Common handler for file uploads."""
    ctx = await _get_upload_context(message)
    if ctx is None:
        return
    _settings, _session_manager, _claude_bridge, bot, session = ctx

    topic_id = message.message_thread_id

    # Имя из Telegram недоверенное — чистим до базового имени (path traversal).
    file_name = _safe_upload_name(file_name, "document")

    # Download file
    try:
        with tempfile.TemporaryDirectory(prefix="telegram-upload-") as tmp_dir:
            tmp_path = await _download_to_temp_file(
                bot=bot,
                file_id=file_id,
                file_name=file_name,
                destination_dir=Path(tmp_dir),
            )

            # Copy to project directory
            project_path = Path(session.project_path)
            uploads_dir = project_path / ".telegram-uploads"
            dest_path = uploads_dir / file_name
            # Финальная страховка: destination обязан лежать внутри uploads_dir.
            if not is_path_within_root(uploads_dir, dest_path):
                raise RuntimeError("unsafe upload path rejected")
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            # Move file
            tmp_path.rename(dest_path)

            logger.info(
                "file_uploaded",
                file_name=file_name,
                dest=str(dest_path),
            )

    except Exception as e:
        logger.error("file_download_failed", error=str(e))
        await message.answer(f"Ошибка при скачивании файла: {e}")
        return

    # Build prompt with file reference
    caption = message.caption or ""
    prompt = f"Я загрузил(а) файл: {dest_path}\n\n{caption}".strip()

    if not caption:
        prompt = f"Я загрузил(а) файл по пути {dest_path}. Пожалуйста, проанализируй его."

    await _process_prompt(message, topic_id, session, prompt)


async def _handle_image_upload(
    message: Message,
    file_id: str,
    file_name: str,
    mime_type: str,
) -> None:
    """Download an image, encode it, and send it for vision analysis."""
    ctx = await _get_upload_context(message)
    if ctx is None:
        return
    _settings, _session_manager, _claude_bridge, bot, session = ctx

    topic_id = message.message_thread_id

    # Имя из Telegram недоверенное — чистим до базового имени (path traversal).
    file_name = _safe_upload_name(file_name, "image.png")

    try:
        with tempfile.TemporaryDirectory(prefix="telegram-image-") as tmp_dir:
            tmp_path = await _download_to_temp_file(
                bot=bot,
                file_id=file_id,
                file_name=file_name,
                destination_dir=Path(tmp_dir),
            )

            project_path = Path(session.project_path)
            images_dir = project_path / ".telegram-uploads" / "images"
            dest_path = images_dir / file_name
            if not is_path_within_root(images_dir, dest_path):
                raise RuntimeError("unsafe upload path rejected")
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.rename(dest_path)

            base64_data = base64.b64encode(dest_path.read_bytes()).decode("ascii")
            image_kind = _infer_image_kind(message, file_name)
            attachment = ClaudeImageAttachment(
                source_path=str(dest_path.relative_to(project_path)),
                file_name=file_name,
                mime_type=mime_type,
                base64_data=base64_data,
                kind=image_kind,
            )

            logger.info(
                "image_uploaded",
                file_name=file_name,
                dest=str(dest_path),
                image_kind=image_kind,
                base64_chars=len(base64_data),
            )

    except Exception as e:
        logger.error("image_download_failed", error=str(e))
        await message.answer(f"Ошибка при скачивании изображения: {e}")
        return

    prompt = _build_image_prompt(message, attachment.source_path, attachment.kind)
    await _process_prompt(message, topic_id, session, prompt, attachments=[attachment])


async def _process_prompt(
    message: Message,
    topic_id: int,
    session,
    prompt: str,
    attachments: list[ClaudeImageAttachment] | None = None,
) -> None:
    """Send a prepared prompt to Claude and stream the response."""
    settings: Settings = router.settings  # type: ignore
    session_manager: SessionManager = router.session_manager  # type: ignore
    claude_bridge: ClaudeBridge = router.claude_bridge  # type: ignore
    streamer: ResponseStreamer = router.streamer  # type: ignore
    event_bus: EventBus | None = getattr(router, "event_bus", None)  # type: ignore

    state = await streamer.create_log_message(
        chat_id=message.chat.id,
        topic_id=topic_id,
    )

    response_text = ""
    usage_data = None

    try:
        if event_bus is not None:
            request_id = uuid.uuid4().hex
            error_text: str | None = None

            async def on_update(event: AgentStreamingUpdate) -> None:
                nonlocal response_text
                if event.request_id != request_id:
                    return
                if event.kind == "log" and settings.display.show_logs:
                    await streamer.update_log(state, event.content)
                elif event.kind == "text":
                    response_text += event.content
                    await streamer.stream_response(state, event.content)
                elif event.kind == "init":
                    session_id = event.metadata.get("session_id")
                    if session_id:
                        await session_manager.async_update_session_id(topic_id, session_id)

            async def on_finished(event: AgentFinished) -> None:
                nonlocal usage_data, error_text
                if event.request_id != request_id:
                    return
                usage_data = event.usage
                if event.session_id:
                    await session_manager.async_update_session_id(topic_id, event.session_id)
                if event.error:
                    error_text = event.error

            # Реальный отправитель (не id группы) — чтобы per-user MCP-подключения
            # применились и к сообщениям с файлом, и чтобы relay корректно решил
            # SP2 auth-gate. Без `privileged` (default False) загрузка файла
            # admin'ом была бы ошибочно отклонена при require_user_key=true.
            auth_user_id = message.from_user.id if message.from_user else 0
            # Privileged = whitelist OR is_admin (parity with web / messages.py).
            # The whitelisted owner rides their own subscription even though
            # Telegram login yields is_admin=0.
            allowed_ids = getattr(settings, "get_allowed_user_ids", list)()
            is_privileged = await is_bot_privileged(
                session_manager, auth_user_id, allowed_ids
            )

            unsubscribe_update = event_bus.subscribe(AgentStreamingUpdate, on_update)
            unsubscribe_finished = event_bus.subscribe(AgentFinished, on_finished)
            try:
                await event_bus.publish(
                    UserMessageReceived(
                        request_id=request_id,
                        chat_id=message.chat.id,
                        user_id=auth_user_id,
                        topic_id=topic_id,
                        project_path=session.project_path,
                        project_name=session.project_name,
                        text=prompt,
                        session_id=session.session_id,
                        attachments=attachments or [],
                        privileged=is_privileged,
                    )
                )
            finally:
                unsubscribe_update()
                unsubscribe_finished()

            if error_text:
                await message.answer(f"Ошибка: {error_text}")
                return
        else:
            # Direct-bridge fallback (no relay). Dead in production — core.py
            # always wires event_bus, so the SP2 auth gate runs in the relay via
            # the `privileged=` field published above. But if a future change
            # leaves event_bus=None, a file/image upload must NOT silently spawn
            # on the OWNER's credentials and bypass per-user billing: resolve the
            # same gate here and inject the caller's key (parity with the
            # direct-bridge path in messages.py). On a hard refusal we prompt for
            # /apikey and return without spawning Claude.
            auth_user_id = message.from_user.id if message.from_user else 0
            api_key_store = getattr(router, "api_key_store", None)
            allowed_ids = getattr(settings, "get_allowed_user_ids", list)()
            is_privileged = await is_bot_privileged(
                session_manager, auth_user_id, allowed_ids
            )
            user_key = (
                api_key_store.get_key(auth_user_id)
                if api_key_store is not None
                else None
            )
            require_user_key = (
                bool(getattr(settings, "require_user_key", True))
                if api_key_store is not None
                else False
            )
            try:
                auth_decision = resolve_session_auth(
                    is_privileged=is_privileged,
                    user_key=user_key,
                    require_user_key=require_user_key,
                )
            except NeedsApiKeyError:
                await message.answer(
                    "Добавьте свой Anthropic API-ключ через команду:\n/apikey"
                )
                return

            async for event in claude_bridge.send_message(
                message=prompt,
                topic_id=topic_id,
                project_path=session.project_path,
                session_id=session.session_id,
                attachments=attachments,
                anthropic_api_key=auth_decision.api_key,
            ):
                if event.type == ClaudeEventType.LOG:
                    if settings.display.show_logs:
                        await streamer.update_log(state, event.content)

                elif event.type == ClaudeEventType.TEXT:
                    response_text += event.content
                    await streamer.stream_response(state, event.content)

                elif event.type == ClaudeEventType.COMPLETE:
                    if event.metadata:
                        usage_data = event.metadata.get("usage")
                        if event.metadata.get("session_id"):
                            await session_manager.async_update_session_id(
                                topic_id,
                                event.metadata["session_id"],
                            )

                elif event.type == ClaudeEventType.ERROR:
                    await message.answer(f"Ошибка: {event.content}")
                    return

    except Exception as e:
        logger.error("claude_error", error=str(e))
        await message.answer(f"Ошибка: {e}")
        return

    await streamer.finalize(
        state,
        show_token_usage=settings.display.show_token_usage,
        show_context_usage=settings.display.show_context_usage,
        usage=usage_data,
    )


async def _download_to_temp_file(
    *,
    bot: Bot,
    file_id: str,
    file_name: str,
    destination_dir: Path,
) -> Path:
    """Download a Telegram file into a temporary directory."""
    file = await bot.get_file(file_id)
    if not file.file_path:
        raise RuntimeError("Не удалось скачать файл.")

    destination_dir.mkdir(parents=True, exist_ok=True)
    # Имя недоверенное (voice/audio тоже идут сюда) — чистим, чтобы не
    # вырваться даже из временного каталога.
    dest_path = destination_dir / _safe_upload_name(file_name, "download")
    with open(dest_path, "wb") as tmp:
        await bot.download_file(file.file_path, tmp)
    return dest_path


async def _transcribe_voice_message(voice_path: Path) -> str:
    """Transcribe a voice file using the local whisper CLI."""
    if shutil.which("whisper") is None:
        raise WhisperCLIUnavailableError("whisper")

    process = await asyncio.create_subprocess_exec(
        "whisper",
        str(voice_path),
        "--model",
        "small",
        "--output_format",
        "txt",
        "--output_dir",
        str(voice_path.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        details = stderr.decode().strip() or stdout.decode().strip() or "unknown error"
        raise RuntimeError(details)

    transcript_path = voice_path.with_suffix(".txt")
    if not transcript_path.exists():
        raise RuntimeError("whisper не создал файл транскрипции")

    transcript = transcript_path.read_text(encoding="utf-8").strip()
    transcript_path.unlink(missing_ok=True)

    if not transcript:
        raise RuntimeError("whisper не вернул текст")

    return transcript


def _build_voice_prompt(message: Message, recognized_text: str) -> str:
    """Build a Claude prompt from reply context, caption, and transcription."""
    parts: list[str] = []

    reply_text, caption = _extract_reply_context(message)
    if reply_text:
        parts.append(f"Контекст ответа:\n{reply_text}")
    if caption:
        parts.append(caption)

    parts.append(recognized_text)
    return "\n\n".join(part for part in parts if part.strip())


def _build_image_prompt(message: Message, image_path: str, image_kind: str) -> str:
    """Build a Claude prompt for image analysis."""
    parts: list[str] = []

    reply_text, caption = _extract_reply_context(message)
    if reply_text:
        parts.append(f"Контекст ответа:\n{reply_text}")
    if caption:
        parts.append(caption)

    parts.append(
        f"Это {image_kind}. Открой файл {image_path} в рабочей директории и проанализируй изображение."
    )
    return "\n\n".join(part for part in parts if part.strip())


def _infer_image_kind(message: Message, file_name: str) -> str:
    """Infer a lightweight image type label from caption or filename."""
    haystack = " ".join(
        filter(
            None,
            [
                (getattr(message, "caption", None) or ""),
                file_name,
            ],
        )
    ).lower()

    diagram_keywords = ("diagram", "chart", "graph", "flow", "schema", "wireframe")
    if any(keyword in haystack for keyword in diagram_keywords):
        return "diagram"

    screenshot_keywords = ("screenshot", "mockup", "ui", "layout", "screen")
    if any(keyword in haystack for keyword in screenshot_keywords):
        return "screenshot"

    return "screenshot"


def _is_image_document(document) -> bool:
    """Return True when a Telegram document looks like an image."""
    mime_type = getattr(document, "mime_type", "") or ""
    if mime_type.startswith("image/"):
        return True

    file_name = getattr(document, "file_name", "") or ""
    return Path(file_name).suffix.lower() in _IMAGE_EXTENSIONS
