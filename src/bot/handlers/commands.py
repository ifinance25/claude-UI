"""Command handlers (/start, /close, /settings, etc.)."""
from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import structlog

from src.bot.keyboards import (
    create_close_confirm_keyboard,
    create_mcp_catalog_keyboard,
    create_project_keyboard,
    create_settings_keyboard,
)
from src.claude import SessionManager, SessionStatus, ClaudeBridge
from src.claude.mcp import McpManager, get_mcp_manager
from src.bot import onboarding
from src.config import Settings
from src.utils.url_safety import is_loopback_url, is_public_https_url
from src.utils.context_hint import CONTEXT_WINDOW

logger = structlog.get_logger()

router = Router(name="commands")


def setup_command_handlers(
    dp_router: Router,
    settings: Settings,
    session_manager: SessionManager,
    claude_bridge: ClaudeBridge,
    web_server: object | None = None,
) -> None:
    """Register command handlers with the router."""
    router.settings = settings  # type: ignore
    router.session_manager = session_manager  # type: ignore
    router.claude_bridge = claude_bridge  # type: ignore
    router.mcp_manager = get_mcp_manager()  # type: ignore
    router.web_server = web_server  # type: ignore

    dp_router.include_router(router)


def parse_continue_payload(args: str | None) -> str | None:
    """Из payload deeplink `/start continue_<uuid>` достаёт uuid; иначе None."""
    if not args or not args.startswith("continue_"):
        return None
    uuid = args[len("continue_") :].strip()
    return uuid or None


async def _continue_in_topic(
    *, bot, session_manager, claude_bridge, uuid: str, from_user_id: int, chat_id: int
) -> None:
    """Создаёт форум-топик для веб-сессии ``uuid`` и переносит Claude session_id.

    Резюме контекста идёт через ``session_id`` (его несёт ``--resume``), а не
    через topic_id: веб-сессия остаётся, создаётся новая топик-сессия с тем же
    ``session_id``. Чтобы веб-сессия и новый топик не дописывали один общий
    transcript-файл (у них один ``session_id``), топик помечается
    ``mark_fork_pending`` — первый resume форкнет сессию в изолированный
    session_id. Чужая/несуществующая сессия → отказ (owner-check). Если
    форум-топик не создался (форум выключен) — мягкий fallback в основной чат.
    """
    # _TOPIC_COLORS/_get_icon_emoji_id — переиспользуем из messages-хендлера
    # (тот же стиль авто-создания топика). Импорт локальный, чтобы избежать
    # любой возможной циклической зависимости на уровне модуля.
    from src.bot.handlers.messages import _TOPIC_COLORS, _get_icon_emoji_id

    session = await session_manager.async_get_session_by_uuid(
        uuid, owner_chat_id=from_user_id, touch=False
    )
    if session is None:
        # Наблюдаемость: фиксируем отказ (чужой/несуществующий uuid). Ответ
        # одинаков для «нет» и «не твоё» — оракула существования не создаём.
        logger.warning("continue_deeplink_denied", from_user_id=from_user_id)
        await bot.send_message(chat_id=chat_id, text="Сессия не найдена или недоступна.")
        return

    name = (
        f"↩️ {session.project_name}"
        if session.project_name
        else "↩️ Продолжение из веба"
    )[:128]

    # Шаг 1 — создать форум-топик. Может упасть, если форум выключен.
    try:
        kwargs: dict = {
            "chat_id": chat_id,
            "name": name,
            "icon_color": _TOPIC_COLORS[chat_id % len(_TOPIC_COLORS)],
        }
        emoji_id = await _get_icon_emoji_id(bot, name)
        if emoji_id:
            kwargs["icon_custom_emoji_id"] = emoji_id
        new_topic = await bot.create_forum_topic(**kwargs)
    except Exception as exc:  # noqa: BLE001 — форум мог быть выключен
        logger.error("continue_deeplink_topic_create_failed", error=str(exc), chat_id=chat_id)
        await bot.send_message(
            chat_id=chat_id,
            text="Не удалось создать топик для продолжения. Создайте топик вручную и продолжите там.",
        )
        return

    # Шаг 2 — топик уже создан. Привязать сессию + перенести session_id. При
    # сбое здесь сообщение «не удалось создать топик» было бы ложью (топик
    # есть), поэтому отдельный except: best-effort откат осиротевшего топика и
    # точное сообщение в основной чат.
    topic_id = new_topic.message_thread_id
    try:
        await session_manager.async_create_session(
            topic_id, session.project_path, session.project_name, chat_id
        )
        if session.session_id:
            await session_manager.async_update_session_id(topic_id, session.session_id)
            claude_bridge.mark_fork_pending(topic_id)
            text = (
                f"↩️ Продолжаю сессию из веба · <b>{escape(session.project_name or '')}</b>.\n"
                "Контекст восстановлен — пишите дальше."
            )
        else:
            text = (
                f"Проект <b>{escape(session.project_name or '')}</b> открыт. "
                "В вебе ещё не было диалога — начните здесь."
            )
        await bot.send_message(
            chat_id=chat_id, message_thread_id=topic_id, text=text, parse_mode="HTML"
        )
    except Exception as exc:  # noqa: BLE001 — сбой уже ПОСЛЕ создания топика
        logger.error("continue_deeplink_resume_failed", error=str(exc), topic_id=topic_id)
        # Best-effort откат, чтобы не оставлять мусорный пустой топик/сессию.
        try:
            await session_manager.async_close_session(topic_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=topic_id)
        except Exception:  # noqa: BLE001
            pass
        # Сообщение в ОСНОВНОЙ чат: топик мог быть удалён, и send в него мог
        # быть как раз тем, что упало.
        await bot.send_message(
            chat_id=chat_id,
            text="Не удалось восстановить сессию из веба. Попробуйте ещё раз позже.",
        )


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    """Handle /start command (+ deeplink continue_<uuid>)."""
    uuid = parse_continue_payload(command.args)
    if uuid:
        session_manager: SessionManager = router.session_manager  # type: ignore
        claude_bridge: ClaudeBridge = router.claude_bridge  # type: ignore
        await _continue_in_topic(
            bot=message.bot,
            session_manager=session_manager,
            claude_bridge=claude_bridge,
            uuid=uuid,
            from_user_id=message.from_user.id if message.from_user else 0,
            chat_id=message.chat.id,
        )
        return
    logger.info(
        "🚀 /start",
        user=message.from_user.username if message.from_user else "unknown",
        chat_id=message.chat.id,
    )
    await message.answer(onboarding.start_message(), parse_mode="HTML")


@router.message(Command("auth"))
async def cmd_auth(message: Message) -> None:
    """Handle /auth command — show auth instructions."""
    logger.info("/auth", user=message.from_user.username if message.from_user else "unknown")
    await message.answer(onboarding.auth_message(), parse_mode="HTML")


@router.message(Command("close"))
async def cmd_close(message: Message) -> None:
    """Handle /close command - close current session."""
    topic_id = message.message_thread_id
    logger.info("/close", topic_id=topic_id, user=message.from_user.username if message.from_user else "unknown")

    if not topic_id:
        await message.answer("Эта команда работает только внутри Топика.")
        return

    session_manager: SessionManager = router.session_manager  # type: ignore

    session = await session_manager.async_get_session(topic_id)
    if not session:
        await message.answer("В этом топике нет активной сессии.")
        return

    project_name = session.project_name

    await message.answer(
        f"Закрыть сессию для <b>{project_name}</b>?\n\n"
        "Это закроет данный топик и удалит сессию.",
        reply_markup=create_close_confirm_keyboard(),
        parse_mode="HTML",
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    """Handle /settings command."""
    settings: Settings = router.settings  # type: ignore
    session_manager: SessionManager = router.session_manager  # type: ignore
    session = (
        await session_manager.async_get_session(message.message_thread_id)
        if message.message_thread_id
        else None
    )
    enable_subagent_tracking = (
        session.enable_subagent_tracking if session else False
    )

    await message.answer(
        "\u2699\ufe0f <b>Настройки бота</b>",
        reply_markup=create_settings_keyboard(
            settings.display.show_token_usage,
            enable_subagent_tracking,
            settings.display.show_context_usage,
            settings.display.keep_log_after_response,
        ),
        parse_mode="HTML",
    )


@router.message(Command("mcp"))
async def cmd_mcp(message: Message) -> None:
    """Handle /mcp command - show catalog or install/remove tools."""
    manager: McpManager = router.mcp_manager  # type: ignore
    parts = message.text.split(maxsplit=2) if message.text else []
    subcommand = parts[1].lower() if len(parts) > 1 else ""

    if not subcommand:
        await message.answer(
            manager.describe_catalog(),
            reply_markup=create_mcp_catalog_keyboard(manager.installed_server_names()),
            parse_mode="HTML",
        )
        return

    if subcommand == "install":
        if len(parts) < 3:
            await message.answer(
                "Использование: <code>/mcp install &lt;npm|npx|github|path&gt;</code>",
                parse_mode="HTML",
            )
            return
        try:
            result = manager.install(parts[2])
        except ValueError as exc:
            await message.answer(f"❌ {escape(str(exc))}", parse_mode="HTML")
            return
        await message.answer(result.message, parse_mode="HTML")
        return

    if subcommand in {"remove", "rm", "uninstall"}:
        if len(parts) < 3:
            await message.answer(
                "Использование: <code>/mcp remove &lt;name&gt;</code>",
                parse_mode="HTML",
            )
            return
        result = manager.remove(parts[2].strip())
        await message.answer(result.message, parse_mode="HTML")
        return

    await message.answer(
        "Доступно: <code>/mcp</code>, <code>/mcp install ...</code>, <code>/mcp remove ...</code>",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle /status command - show current session status."""
    topic_id = message.message_thread_id
    logger.info("/status", topic_id=topic_id)
    session_manager: SessionManager = router.session_manager  # type: ignore

    if not topic_id:
        sessions = await session_manager.async_get_all_sessions()
        if not sessions:
            await message.answer("Нет активных сессий.")
            return

        lines = ["<b>Активные сессии:</b>\n"]
        for s in sessions:
            lines.append(f"\u2022 <b>{s.project_name}</b> (топик {s.topic_id})")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    session = await session_manager.async_get_session(topic_id)
    if not session:
        await message.answer("В этом топике нет сессии. Отправьте сообщение, чтобы начать.")
        return

    status = SessionStatus(session.status) if hasattr(session, "status") else SessionStatus.NEW
    verbose_names = {0: "🔇 Тихий", 1: "🔈 Нормальный", 2: "🔊 Подробный", 3: "📋 Детальный"}
    verbose_level = session.verbose_level if hasattr(session, "verbose_level") else 1

    # Build status lines
    lines = [
        "<b>Текущая сессия</b>\n",
        f"Проект: <b>{session.project_name}</b>",
        f"Путь: <code>{session.project_path}</code>",
        f"ID Сессии: <code>{session.session_id or 'Новая'}</code>",
        f"Создана: {session.created_at.strftime('%Y-%m-%d %H:%M')}",
        f"Статус: {status.emoji} {status.value}",
        f"Детализация: {verbose_names.get(verbose_level, str(verbose_level))}",
    ]

    # Add context usage if enabled
    settings: Settings = router.settings  # type: ignore
    if settings.display.show_context_usage:
        context_tokens = (
            session.total_input_tokens
            + session.total_cache_read_tokens
            + session.total_cache_creation_tokens
        )
        context_limit = CONTEXT_WINDOW
        context_percent = min(100, round(context_tokens / context_limit * 100, 1))
        lines.append(
            f"\n📊 <b>Контекст:</b> {context_tokens:,} / {context_limit:,} ({context_percent}%)"
        )
        lines.append(
            f"  • Вход: {session.total_input_tokens:,} | "
            f"Кэш (read): {session.total_cache_read_tokens:,} | "
            f"Кэш (write): {session.total_cache_creation_tokens:,}"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("projects"))
async def cmd_projects(message: Message) -> None:
    """Handle /projects command - list available projects."""
    logger.info("/projects", user=message.from_user.username if message.from_user else "unknown")
    settings: Settings = router.settings  # type: ignore
    projects = settings.get_project_paths()

    if not projects:
        await message.answer(
            onboarding.no_projects_message(settings.get_projects_directory()),
            parse_mode="HTML",
        )
        return

    await message.answer(
        "<b>Доступные проекты:</b>\n\n"
        "Выберите проект для переключения:",
        reply_markup=create_project_keyboard(projects),
        parse_mode="HTML",
    )


@router.message(Command("verbose"))
async def cmd_verbose(message: Message) -> None:
    """Handle /verbose command - set detail level for streaming output."""
    topic_id = message.message_thread_id
    if not topic_id:
        await message.answer("Команда работает только в Топике.")
        return
    session_manager: SessionManager = router.session_manager  # type: ignore
    args = message.text.split(maxsplit=1) if message.text else []
    if len(args) < 2:
        session = await session_manager.async_get_session(topic_id)
        level = session.verbose_level if session else 1
        names_full = {0: "Тихий", 1: "Нормальный", 2: "Подробный", 3: "Детальный"}
        await message.answer(
            f"📊 Уровень детализации: <b>{names_full[level]}</b> ({level})\n\n"
            "Изменить: /verbose 0, /verbose 1, /verbose 2, /verbose 3",
            parse_mode="HTML",
        )
        return
    try:
        level = int(args[1].strip())
        if level < 0 or level > 3:
            raise ValueError
    except ValueError:
        await message.answer("Используйте: /verbose 0, /verbose 1, /verbose 2 или /verbose 3")
        return
    await session_manager.async_set_verbose_level(topic_id, level)
    names_full = {
        0: "🔇 Тихий — только финальный ответ",
        1: "🔈 Нормальный — инструменты и текст",
        2: "🔊 Подробный — полный вывод инструментов",
        3: "📋 Детальный — аргументы + статус",
    }
    await message.answer(f"✅ {names_full[level]}", parse_mode="HTML")


@router.message(Command("stop"))
async def cmd_stop(message: Message) -> None:
    """Handle /stop command - cancel active Claude generation."""
    topic_id = message.message_thread_id
    logger.info("/stop", topic_id=topic_id, user=message.from_user.username if message.from_user else "unknown")

    claude_bridge: ClaudeBridge = router.claude_bridge  # type: ignore
    session_manager: SessionManager = router.session_manager  # type: ignore

    cancelled = await claude_bridge.cancel_message(topic_id or 0)
    if cancelled:
        if topic_id:
            await session_manager.async_set_status(topic_id, SessionStatus.STOPPED)
        await message.answer("🛑 Генерация остановлена.")
    else:
        await message.answer("Нет активных задач.")


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    """Handle /usage command - show token usage for current session."""
    topic_id = message.message_thread_id
    session_manager: SessionManager = router.session_manager  # type: ignore

    if not topic_id:
        sessions = await session_manager.async_get_all_sessions()
        if not sessions:
            await message.answer("Нет активных сессий.")
            return
        total_cost = sum(s.total_cost_usd for s in sessions)
        total_input = sum(s.total_input_tokens for s in sessions)
        total_output = sum(s.total_output_tokens for s in sessions)
        total_cache = sum(s.total_cache_read_tokens for s in sessions)
        await message.answer(
            "<b>Суммарное использование (все сессии)</b>\n\n"
            f"Сессий: <b>{len(sessions)}</b>\n"
            f"Input: <code>{total_input:,}</code> токенов\n"
            f"Output: <code>{total_output:,}</code> токенов\n"
            f"Cache read: <code>{total_cache:,}</code> токенов\n"
            f"Стоимость: <b>${total_cost:.4f}</b>",
            parse_mode="HTML",
        )
        return

    session = await session_manager.async_get_session(topic_id)
    if not session:
        await message.answer("В этом топике нет сессии.")
        return

    context_tokens = session.total_input_tokens + session.total_cache_read_tokens
    await message.answer(
        f"<b>Использование — {session.project_name}</b>\n\n"
        f"Сообщений: <b>{session.message_count}</b>\n"
        f"Input: <code>{session.total_input_tokens:,}</code> токенов\n"
        f"Output: <code>{session.total_output_tokens:,}</code> токенов\n"
        f"Cache read: <code>{session.total_cache_read_tokens:,}</code>\n"
        f"Cache creation: <code>{session.total_cache_creation_tokens:,}</code>\n"
        f"Контекст: <code>{context_tokens:,}</code> токенов\n"
        f"Стоимость: <b>${session.total_cost_usd:.4f}</b>",
        parse_mode="HTML",
    )


@router.message(Command("skills"))
async def cmd_skills(message: Message) -> None:
    """Handle /skills command - list all available slash commands."""
    await message.answer(
        "<b>Доступные команды</b>\n\n"
        "<b>Команды бота:</b>\n"
        "/start — Запустить бота\n"
        "/projects — Список проектов\n"
        "/status — Статус сессии\n"
        "/settings — Настройки\n"
        "/verbose — Уровень детализации (0/1/2/3)\n"
        "/stop — Прервать генерацию\n"
        "/close — Закрыть сессию\n"
        "/usage — Статистика токенов\n"
        "/auth — Авторизация\n"
        "/weblogin — Ссылка для входа в веб-интерфейс\n"
        "/help — Справка\n\n"
        "<b>Команды Claude Code:</b>\n"
        "/compact — Сжать историю диалога\n"
        "/clear — Очистить историю\n"
        "/cost — Стоимость и токены\n"
        "/context — Использование контекста\n"
        "/diff — Незакоммиченные изменения\n"
        "/commit — Создать коммит\n"
        "/review — Ревью PR\n"
        "/init — Инициализировать CLAUDE.md\n"
        "/model — Показать/сменить модель\n"
        "/mcp — MCP-серверы\n"
        "/config — Настройки Claude Code\n"
        "/permissions — Режим разрешений\n"
        "/export — Экспорт диалога\n"
        "/memory — Управление памятью\n\n"
        "<i>Команды Claude Code выполняются в контексте текущего проекта.</i>",
        parse_mode="HTML",
    )


@router.message(Command("weblogin"))
async def cmd_weblogin(message: Message) -> None:
    """Issue a one-time magic link for the web UI.

    Replaces the static WEB_DEV_BEARER_TOKEN flow with single-use tokens
    (15-minute TTL) bound to the requesting Telegram user. Falls back to
    SSH-tunnel-only mode with a warning when ``web.public_origin`` is
    not configured.
    """
    if message.from_user is None:
        return
    user_id = message.from_user.id
    logger.info("/weblogin", user_id=user_id)

    web_server = getattr(router, "web_server", None)
    if web_server is None or not hasattr(web_server, "issue_magic_login_url"):
        await message.answer(
            "🛑 Web UI не сконфигурирован на этом боте. "
            "Включите <code>web.enabled: true</code> в config.yaml и перезапустите.",
            parse_mode="HTML",
        )
        return

    url = web_server.issue_magic_login_url(user_id)
    safe_url = escape(url)
    # Telegram отказывается принимать localhost / http URL в inline-
    # кнопках (TelegramBadRequest: Wrong HTTP URL). Для локальной
    # разработки шлём URL в <code> блоке (tap-to-copy), для прода с
    # настоящим HTTPS public_origin — добавляем удобную inline-кнопку.
    # is_public_https_url знает про IPv6 ::1 и единственный источник
    # правды для loopback-проверки во всём проекте.
    if is_public_https_url(url):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌐 Открыть Web UI", url=url)]
            ]
        )
        await message.answer(
            "🔗 <b>Magic-link для входа в Web UI</b>\n\n"
            f"<code>{safe_url}</code>\n\n"
            "⏱ Действительна 15 минут, одноразовая.\n"
            "🔒 Не пересылайте — кто откроет первым, тот и войдёт под вашим аккаунтом.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
    else:
        # Локальный dev (http://localhost...): Telegram не сделает URL
        # кликабельным, но юзер может скопировать <code> блок одним
        # тапом и вставить в браузер.
        lines = [
            "🔗 <b>Magic-link для входа в Web UI</b>",
            "",
            f"<code>{safe_url}</code>",
        ]
        # L-9: web.public_origin не задан (штатная установка всегда его
        # задаёт — это ручные/Telegram-only конфиги) → ссылка молча указывала
        # на 127.0.0.1/localhost. С телефона (не с самого сервера и не через
        # SSH-туннель) такая ссылка ведёт в никуда — предупреждаем явно.
        if is_loopback_url(url):
            lines.append(
                "⚠️ Ссылка работает только на самом сервере или через "
                "SSH-туннель. Задайте <code>web.public_origin</code> "
                "(публичный адрес), чтобы ссылка открывалась с телефона."
            )
        lines += [
            "",
            "📋 Тапните по ссылке, чтобы скопировать, и откройте в браузере.",
            "⏱ Действительна 15 минут, одноразовая.",
            "🔒 Не пересылайте — кто откроет первым, тот и войдёт под вашим аккаунтом.",
        ]
        await message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    logger.info("/help", user=message.from_user.username if message.from_user else "unknown")
    await message.answer(
        "<b>Команды Claude Code</b>\n\n"
        "Здесь работают все стандартные команды Claude:\n"
        "/commit - Создать коммит\n"
        "/clear - Очистить историю\n"
        "/compact - Сжать диалог\n"
        "/review - Сделать код-ревью PR\n"
        "/context - Использование контекста\n"
        "/cost - Статистика токенов\n"
        "/config - Настройки Claude Code\n"
        "/mcp - MCP-серверы\n"
        "/model - Выбор модели\n"
        "И многие другие!\n\n"
        "<b>Команды бота:</b>\n"
        "/start - Запустить\n"
        "/close - Закрыть текущую сессию\n"
        "/settings - Настройки\n"
        "/status - Статус сессии\n"
        "/projects - Выбрать проект\n"
        "/auth - Авторизация\n"
        "/weblogin - Ссылка для входа в веб-интерфейс\n"
        "/help - Эта справка",
        parse_mode="HTML",
    )
