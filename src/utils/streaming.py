"""Streaming response handler using Telegram sendMessageDraft API.

Simple approach: push all received text as draft every 150ms.
No character simulation — sendMessageDraft handles visual smoothness natively.
"""
from __future__ import annotations

import asyncio
import html as html_mod
import random
import re
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.types import BufferedInputFile, Message

import structlog

from src.utils.context_hint import CONTEXT_WINDOW, ContextHintGate
from src.utils.formatter import format_for_telegram, strip_html_tags

logger = structlog.get_logger()


@dataclass
class StreamingState:
    chat_id: int
    topic_id: int | None
    log_message: Message | None = None
    response_message: Message | None = None
    log_buffer: str = ""
    response_buffer: str = ""
    last_log_update: float = 0
    message_parts: list[Message] = field(default_factory=list)
    _draft_id: int = 0
    _draft_task: asyncio.Task | None = None
    _receiving: bool = True
    _done_event: asyncio.Event = field(default_factory=asyncio.Event)
    tool_header_lines: list = field(default_factory=list)
    _last_usage: dict | None = None  # type: ignore


class ResponseStreamer:
    """Handles streaming responses to Telegram using sendMessageDraft."""

    DRAFT_INTERVAL = 0.03  # 30ms — maximum speed, no rate limit on drafts

    def __init__(
        self,
        bot: Bot,
        max_message_length: int = 4096,
        log_update_interval_ms: int = 1000,
        code_as_file_threshold: int = 500,
    ):
        self.bot = bot
        self.max_length = max_message_length
        self.log_interval = log_update_interval_ms / 1000
        self.code_threshold = code_as_file_threshold
        # Per-topic gate для одноразовой подсказки «контекст почти полон».
        self._ctx_gate = ContextHintGate()

    async def create_log_message(
        self,
        chat_id: int,
        topic_id: int | None = None,
    ) -> StreamingState:
        msg = await self.bot.send_message(
            chat_id,
            "\u23f3 Обработка...",
            message_thread_id=topic_id,
        )
        state = StreamingState(
            chat_id=chat_id,
            topic_id=topic_id,
            log_message=msg,
            last_log_update=time.time(),
            _draft_id=random.randint(1, 2**31 - 1),
        )
        # Start background draft pusher
        state._draft_task = asyncio.create_task(self._draft_loop(state))
        return state

    async def update_log(self, state: StreamingState, log_text: str) -> None:
        state.log_buffer = log_text
        now = time.time()
        if now - state.last_log_update < self.log_interval:
            return
        if state.log_message:
            try:
                display = state.log_buffer[-(self.max_length - 100):]
                if len(state.log_buffer) > self.max_length - 100:
                    display = "..." + display
                # Use plain text — emojis and formatting look better without <pre>
                await state.log_message.edit_text(
                    display,
                    parse_mode=None,
                )
                state.last_log_update = now
            except Exception as e:
                if "message is not modified" not in str(e):
                    logger.debug("log_edit_failed", error=str(e))

    async def stream_response(
        self,
        state: StreamingState,
        text_chunk: str,
    ) -> None:
        """Append text from Claude. Draft loop pushes it to Telegram."""
        state.response_buffer += text_chunk

    async def finalize(
        self,
        state: StreamingState,
        show_token_usage: bool = True,
        usage: dict | None = None,
        cumulative: dict | None = None,
        send_empty_completion: bool = True,
        show_context_usage: bool = False,
        keep_log: bool = False,
    ) -> None:
        """Wait for draft to finish, send final formatted message."""
        state._receiving = False

        # Log message has no markup — nothing to remove here

        # Wait for draft loop to push final text (max 3 seconds)
        try:
            await asyncio.wait_for(state._done_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass

        # Stop draft loop
        if state._draft_task:
            state._draft_task.cancel()
            try:
                await state._draft_task
            except asyncio.CancelledError:
                pass

        # Build final text
        if show_token_usage and usage:
            usage_text = self._format_usage(usage, cumulative, show_context_usage)
            if usage_text:
                state.response_buffer += usage_text



        # Send final formatted message, then delete log
        if state.response_buffer:
            await self._send_final_formatted(state)
        elif send_empty_completion:
            try:
                await self.bot.send_message(
                    state.chat_id, "✅ Выполнено.",
                    message_thread_id=state.topic_id,
                )
            except Exception:
                pass

        # Delete or keep log message (after final is sent)
        if state.log_message:
            if keep_log:
                try:
                    final_text = state.log_buffer.replace("\u23f3", "\u2705")
                    await state.log_message.edit_text(final_text, parse_mode=None)
                except Exception:
                    pass
            else:
                try:
                    await state.log_message.delete()
                except Exception:
                    pass

        # Подсказка «контекст почти полон» — один раз за пересечение порога,
        # независимо от тумблера show_context_usage (это safety-нудж, не показ).
        if usage and state.topic_id is not None:
            hint = self._ctx_gate.decide(state.topic_id, usage)
            if hint:
                try:
                    await self.bot.send_message(
                        state.chat_id,
                        hint,
                        message_thread_id=state.topic_id,
                    )
                except Exception:  # noqa: BLE001 — подсказка не критична
                    logger.debug("context_hint_send_failed")

    # ── Draft loop ────────────────────────────────────────────

    async def _draft_loop(self, state: StreamingState) -> None:
        """Push current buffer to Telegram draft every DRAFT_INTERVAL."""
        last_draft_len = 0  # Track raw buffer length that was last drafted
        push_count = 0  # Count successful pushes for debugging
        draft_failed = False  # Track if draft API is unavailable
        response_msg_created = False  # Track if we created a response message in fallback mode

        try:
            while True:
                buf_len = len(state.response_buffer)

                # Detect any new content (buffer grew)
                if buf_len > last_draft_len:
                    # Build display text: keep full buffer, trim only the tail if needed
                    display_text = state.response_buffer
                    if len(display_text) > self.max_length - 50:
                        display_text = display_text[-(self.max_length - 50):]

                    # Apply Telegram HTML formatting
                    try:
                        formatted_text = format_for_telegram(display_text)
                        if not formatted_text:
                            formatted_text = display_text
                    except Exception:
                        formatted_text = display_text

                    # Try draft API first
                    if not draft_failed:
                        try:
                            result = await self.bot.send_message_draft(
                                chat_id=state.chat_id,
                                draft_id=state._draft_id,
                                text=formatted_text,
                                message_thread_id=state.topic_id,
                                parse_mode="HTML",
                            )
                            push_count += 1
                            logger.debug("draft_pushed", count=push_count, buf_len=buf_len, result=result)
                        except Exception as e:
                            # Draft API unavailable - switch to edit mode
                            logger.warning("draft_api_unavailable", error=str(e))
                            draft_failed = True

                    # Fallback: use send_message/edit_message_text if draft failed
                    if draft_failed:
                        try:
                            if not response_msg_created:
                                # Create new message for response
                                state.response_message = await self.bot.send_message(
                                    chat_id=state.chat_id,
                                    text=display_text,
                                    message_thread_id=state.topic_id,
                                    parse_mode="HTML",
                                )
                                response_msg_created = True
                                push_count += 1
                                logger.debug("response_msg_created", count=push_count, buf_len=buf_len)
                            elif state.response_message:
                                # Edit existing response message
                                await state.response_message.edit_text(
                                    display_text,
                                    parse_mode="HTML",
                                )
                                push_count += 1
                                logger.debug("response_msg_edited", count=push_count, buf_len=buf_len)
                        except Exception as e2:
                            if "message is not modified" not in str(e2):
                                logger.debug("fallback_edit_error", error=str(e2))

                    last_draft_len = buf_len

                elif not state._receiving:
                    # Claude done + nothing new to push
                    logger.info("draft_loop_done", total_pushes=push_count, draft_failed=draft_failed)
                    state._done_event.set()
                    return

                await asyncio.sleep(self.DRAFT_INTERVAL)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("draft_loop_error", error=str(e))
        finally:
            state._done_event.set()

    # ── Final formatting ──────────────────────────────────────

    async def _send_final_formatted(self, state: StreamingState) -> None:
        text = state.response_buffer
        text, code_files = self._extract_code_blocks(text)
        formatted = format_for_telegram(text)

        # If fallback mode was used (response_message exists), just edit it
        if state.response_message:
            try:
                await state.response_message.edit_text(formatted, parse_mode="HTML")
            except Exception:
                pass
        elif len(formatted) <= self.max_length - 50:
            await self._send_final_message(state, formatted)
        else:
            await self._send_split(state, formatted)

        for filename, code in code_files:
            await self._send_code_file(state, filename, code)

    async def _send_final_message(self, state: StreamingState, html_text: str) -> None:
        display = html_text or "..."
        try:
            state.response_message = await self.bot.send_message(
                state.chat_id, display,
                message_thread_id=state.topic_id,
                parse_mode="HTML",
            )
        except Exception:
            try:
                plain = strip_html_tags(display)
                state.response_message = await self.bot.send_message(
                    state.chat_id, plain or "...",
                    message_thread_id=state.topic_id,
                )
            except Exception as e:
                logger.error("final_send_failed", error=str(e))

    async def _send_split(self, state: StreamingState, text: str) -> None:
        chunks = self._split_text(text)
        for chunk in chunks:
            try:
                msg = await self.bot.send_message(
                    state.chat_id, chunk,
                    message_thread_id=state.topic_id,
                    parse_mode="HTML",
                )
                state.message_parts.append(msg)
            except Exception:
                try:
                    msg = await self.bot.send_message(
                        state.chat_id, strip_html_tags(chunk),
                        message_thread_id=state.topic_id,
                    )
                    state.message_parts.append(msg)
                except Exception:
                    pass

    def _split_text(self, text: str) -> list[str]:
        chunks, current = [], ""
        for para in text.split("\n\n"):
            if len(current) + len(para) + 2 > self.max_length - 100:
                if current:
                    chunks.append(current.strip())
                current = para
                while len(current) > self.max_length - 100:
                    chunks.append(current[:self.max_length - 100])
                    current = current[self.max_length - 100:]
            else:
                current = f"{current}\n\n{para}" if current else para
        if current:
            chunks.append(current.strip())
        return chunks or [""]

    def _extract_code_blocks(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        code_files: list[tuple[str, str]] = []
        pattern = r"```(\w+)?\n(.*?)```"

        def replace(m: re.Match) -> str:
            lang = m.group(1) or "txt"
            code = m.group(2)
            if len(code) > self.code_threshold:
                ext = self._ext(lang)
                filename = f"code.{ext}"
                code_files.append((filename, code))
                return f"[Код отправлен файлом: {filename}]"
            return m.group(0)

        return re.sub(pattern, replace, text, flags=re.DOTALL), code_files

    def _ext(self, lang: str) -> str:
        return {
            "python": "py", "javascript": "js", "typescript": "ts",
            "bash": "sh", "shell": "sh", "json": "json", "yaml": "yaml",
            "html": "html", "css": "css", "sql": "sql", "go": "go",
            "rust": "rs", "java": "java", "cpp": "cpp", "c": "c",
        }.get(lang.lower(), "txt")

    async def _send_code_file(self, state: StreamingState, filename: str, code: str) -> None:
        await self.bot.send_document(
            state.chat_id,
            BufferedInputFile(code.encode("utf-8"), filename=filename),
            message_thread_id=state.topic_id,
        )

    def _format_usage(
        self,
        usage: dict,
        cumulative: dict | None = None,
        show_context_usage: bool = False,
    ) -> str:
        if not isinstance(usage, dict):
            return ""
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        # SDK uses different key names — support both formats
        cache_read = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))
        cache_create = usage.get("cache_creation_input_tokens", usage.get("cache_creation_tokens", 0))

        total = inp + out + cache_read + cache_create
        if total == 0:
            logger.debug("format_usage_skipped", reason="total_is_zero", usage=usage)
            return ""

        lines: list[str] = []

        # Current request usage
        req_parts = [f"in: {inp:,}", f"out: {out:,}"]
        if cache_read > 0:
            req_parts.append(f"cache_read: {cache_read:,}")
        if cache_create > 0:
            req_parts.append(f"cache_write: {cache_create:,}")
        lines.append(f"\n\n📊 Запрос: {' · '.join(req_parts)}")

        # Real context size from Claude (new input + cached context)
        if show_context_usage:
            real_context = inp + cache_read + cache_create
            if real_context > 0:
                context_limit = CONTEXT_WINDOW
                context_percent = min(100, round(real_context / context_limit * 100, 1))
                lines.append(
                    f"\n📈 Контекст: {real_context:,} / {context_limit:,} ({context_percent}%)"
                )
                logger.debug("context_usage_added", real_context=real_context, percent=context_percent)
            else:
                logger.debug("context_usage_skipped", show_context_usage=show_context_usage, real_context=real_context)

        return "".join(lines)
