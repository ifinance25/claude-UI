"""Bridge Claude stream events onto the event bus."""
from __future__ import annotations

import traceback
from typing import Any, Literal

import structlog

from src.apikeys.policy import NeedsApiKeyError, resolve_session_auth
from src.claude.bridge import ClaudeEventType
from src.connections.resolver import build_mcp_servers
from src.event_bus.bus import EventBus
from src.event_bus.events import AgentFinished, AgentStarted, AgentStreamingUpdate, UserMessageReceived

logger = structlog.get_logger(__name__)

# Shown to a caller who must bring their own Anthropic key (Path B) but hasn't.
# Surfaced via AgentFinished.error, the same channel bot/web use for run errors.
NEEDS_API_KEY_MESSAGE = (
    "Добавьте свой Anthropic API-ключ в Настройки → API-ключ, "
    "чтобы отправлять сообщения"
)

# Сколько символов текста копить, прежде чем толкнуть один streaming_update.
# Реальный посимвольный стрим SDK (text_delta) даёт сотни мелких событий на
# длинный ответ и переполнял бы WS-очередь форвардера (maxsize). Коалесцируем
# в чанки ~40 символов: событий в 5-10 раз меньше, а на фронте «печатная
# машинка» (useTypewriter) всё равно сглаживает их посимвольно.
TEXT_FLUSH_CHARS = 40

# Живые «мысли» — такой же высокочастотный посимвольный поток, как текст.
# Коалесцируем по тому же порогу, иначе сотни мелких кадров переполнят
# WS-очередь форвардера → gap → реконнект (а мысли эфемерны, по REST не добрать).
THINKING_FLUSH_CHARS = 40


class ClaudeEventRelay:
    """Subscribes to user requests and republishes Claude stream events."""

    def __init__(
        self,
        *,
        bus: EventBus,
        claude_bridge: Any,
        connections_store: Any = None,
        api_key_store: Any = None,
        settings: Any = None,
    ) -> None:
        self.bus = bus
        self.claude_bridge = claude_bridge
        self._connections_store = connections_store
        # SP2 per-user Anthropic keys. Both are optional so the pre-SP2 wiring
        # (ClaudeEventRelay(bus=..., claude_bridge=...)) keeps working; when
        # absent the auth gate is dormant and every session uses owner creds.
        self.api_key_store = api_key_store
        self.settings = settings
        self._unsubscribe = self.bus.subscribe(UserMessageReceived, self._handle_user_message)

    def close(self) -> None:
        """Detach the relay from the bus."""
        self._unsubscribe()

    async def _handle_user_message(self, event: UserMessageReceived) -> None:
        await self.bus.publish(
            AgentStarted(
                request_id=event.request_id,
                chat_id=event.chat_id,
                topic_id=event.topic_id,
                session_uuid=event.session_uuid,
            )
        )

        # SP2 auth gate: decide which credentials this session runs against
        # BEFORE spawning Claude. Unprivileged callers may be required to bring
        # their own Anthropic key (Path B, "everyone pays with their own key");
        # privileged callers fall back to owner credentials. The store is None
        # when CONNECTIONS_SECRET_KEY is unset, and settings is None on the
        # pre-SP2 wiring — in both cases the gate stays dormant (owner creds).
        user_key: str | None = None
        if self.api_key_store is not None:
            user_key = self.api_key_store.get_key(event.user_id)

        require_user_key = (
            bool(getattr(self.settings, "require_user_key", True))
            if self.api_key_store is not None
            else False
        )
        try:
            auth_decision = resolve_session_auth(
                is_privileged=getattr(event, "privileged", False),
                user_key=user_key,
                require_user_key=require_user_key,
            )
        except NeedsApiKeyError:
            # Refuse fail-closed: no owner-credential fallback for this caller.
            # Surface via AgentFinished.error (bot + web both render it) and skip
            # the Claude run entirely.
            await self.bus.publish(
                AgentFinished(
                    request_id=event.request_id,
                    chat_id=event.chat_id,
                    topic_id=event.topic_id,
                    session_uuid=event.session_uuid,
                    error=NEEDS_API_KEY_MESSAGE,
                )
            )
            return
        api_key_to_inject = auth_decision.api_key

        response_text = ""
        finished = False
        pending_text = ""
        pending_thinking = ""

        async def flush_text() -> None:
            """Сбросить накопленный текст одним streaming_update."""
            nonlocal pending_text
            if pending_text:
                await self._publish_update(event, "text", content=pending_text)
                pending_text = ""

        async def flush_thinking() -> None:
            """Сбросить накопленные мысли одним streaming_update."""
            nonlocal pending_thinking
            if pending_thinking:
                await self._publish_update(event, "thinking", content=pending_thinking)
                pending_thinking = ""

        # Per-user MCP servers (decrypted secrets injected). NEVER log this.
        mcp_servers = None
        if self._connections_store is not None:
            mcp_servers = build_mcp_servers(event.user_id, self._connections_store) or None

        try:
            async for claude_event in self.claude_bridge.send_message(
                message=event.text,
                topic_id=event.topic_id,
                project_path=event.project_path,
                session_id=event.session_id,
                attachments=event.attachments,
                readonly=getattr(event, "readonly", False),
                confine_root=getattr(event, "confine_root", None),
                mcp_servers=mcp_servers,
                anthropic_api_key=api_key_to_inject,
            ):
                if claude_event.type == ClaudeEventType.TEXT:
                    # Сегмент «мыслей» закончился — появился текст ответа.
                    # Сбрасываем накопленные мысли, чтобы порядок в ленте был
                    # «мысли → ответ», а не вперемешку.
                    await flush_thinking()
                    response_text += claude_event.content
                    # Коалесцируем мелкие text_delta в чанки ~TEXT_FLUSH_CHARS,
                    # чтобы не топить WS-очередь форвардера тысячами событий.
                    # Полнота ответа сохраняется (response_text копит всё), а
                    # «печатная машинка» на фронте сгладит чанки посимвольно.
                    pending_text += claude_event.content
                    if len(pending_text) >= TEXT_FLUSH_CHARS:
                        await flush_text()
                    continue

                if claude_event.type == ClaudeEventType.THINKING_DELTA:
                    # Мысль после текста = новый сегмент: сперва сбрасываем
                    # накопленный текст (порядок «ответ → следующая мысль»).
                    await flush_text()
                    # Коалесцируем посимвольные thinking_delta так же, как текст.
                    # В response_text НЕ копим — мысли эфемерны и не должны
                    # утекать в финальный ответ/историю.
                    pending_thinking += claude_event.content
                    if len(pending_thinking) >= THINKING_FLUSH_CHARS:
                        await flush_thinking()
                    continue

                # Любое прочее НЕ-text событие: сперва сбрасываем накопленные
                # текст и мысли, чтобы порядок (мысли → text → tool_use → text)
                # в ленте не ломался.
                await flush_text()
                await flush_thinking()

                if claude_event.type == ClaudeEventType.LOG:
                    await self._publish_update(event, "log", content=claude_event.content)
                    continue

                if claude_event.type == ClaudeEventType.TOOL_USE:
                    # Метадата tool_use (имя tool'а, structured payload
                    # типа AskUserQuestion.questions) пробрасывается
                    # на фронт — без неё фильтр Skill-bubble'ов и
                    # красивый AskUserQuestion-рендер не работают.
                    await self._publish_update(
                        event,
                        "tool_use",
                        content=claude_event.content,
                        metadata=claude_event.metadata or {},
                    )
                    continue

                if claude_event.type == ClaudeEventType.SUBAGENT_START:
                    await self._publish_update(event, "subagent_start", content=claude_event.content)
                    continue

                if claude_event.type == ClaudeEventType.SUBAGENT_LOG:
                    await self._publish_update(event, "subagent_log", content=claude_event.content)
                    continue

                if claude_event.type == ClaudeEventType.SUBAGENT_FINISH:
                    await self._publish_update(event, "subagent_finish", content=claude_event.content)
                    continue

                if claude_event.type == ClaudeEventType.INIT:
                    await self._publish_update(
                        event,
                        "init",
                        metadata=claude_event.metadata or {},
                    )
                    continue

                if claude_event.type == ClaudeEventType.USAGE:
                    await self._publish_update(
                        event,
                        "usage",
                        metadata=claude_event.metadata or {},
                    )
                    continue

                if claude_event.type == ClaudeEventType.COMPLETE:
                    metadata = claude_event.metadata or {}
                    # Add any remaining content from COMPLETE event
                    if claude_event.content:
                        response_text += claude_event.content
                    await self.bus.publish(
                        AgentFinished(
                            request_id=event.request_id,
                            chat_id=event.chat_id,
                            topic_id=event.topic_id,
                            session_uuid=event.session_uuid,
                            session_id=metadata.get("session_id"),
                            usage=metadata.get("usage"),
                            response_text=response_text,
                        )
                    )
                    finished = True
                    continue

                if claude_event.type == ClaudeEventType.ERROR:
                    metadata = claude_event.metadata or {}
                    await self.bus.publish(
                        AgentFinished(
                            request_id=event.request_id,
                            chat_id=event.chat_id,
                            topic_id=event.topic_id,
                            session_uuid=event.session_uuid,
                            session_id=metadata.get("session_id"),
                            usage=metadata.get("usage"),
                            response_text=response_text,
                            error=claude_event.content,
                        )
                    )
                    finished = True
                    break
        except Exception as exc:
            logger.error(
                "claude_relay_exception",
                topic_id=event.topic_id,
                exc_class=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(),
            )
            await self.bus.publish(
                AgentFinished(
                    request_id=event.request_id,
                    chat_id=event.chat_id,
                    topic_id=event.topic_id,
                    session_uuid=event.session_uuid,
                    response_text=response_text,
                    error=str(exc),
                )
            )
            finished = True

        # Поток закончился без явного COMPLETE/ERROR — досбрасываем хвост текста
        # и мыслей, иначе последний чанк (< *_FLUSH_CHARS) не дойдёт до фронта
        # вживую (текст подберёт REST-история; мысли эфемерны — лучше показать).
        await flush_text()
        await flush_thinking()

        if not finished:
            await self.bus.publish(
                AgentFinished(
                    request_id=event.request_id,
                    chat_id=event.chat_id,
                    topic_id=event.topic_id,
                    session_uuid=event.session_uuid,
                    response_text=response_text,
                )
            )

    async def _publish_update(
        self,
        source_event: UserMessageReceived,
        kind: Literal[
            "init",
            "log",
            "tool_use",
            "subagent_start",
            "subagent_log",
            "subagent_finish",
            "text",
            "thinking",
            "usage",
        ],
        *,
        content: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.bus.publish(
            AgentStreamingUpdate(
                request_id=source_event.request_id,
                chat_id=source_event.chat_id,
                topic_id=source_event.topic_id,
                session_uuid=source_event.session_uuid,
                kind=kind,
                content=content,
                metadata=metadata or {},
            )
        )
