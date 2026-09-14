"""Client-facing onboarding copy for Vels Claude Telegram flows."""
from __future__ import annotations

from html import escape
from pathlib import Path


SERVICE_NAME = "vels-claude"


def start_message() -> str:
    return (
        "<b>Vels Claude</b>\n\n"
        "Vels Claude дает доступ к Claude Code прямо из Telegram.\n\n"
        "<b>Как начать:</b>\n"
        "1. Каждый топик - отдельная рабочая сессия.\n"
        "2. Напишите задачу в General chat, и бот создаст топик автоматически.\n"
        "3. Можно также создать топик вручную.\n"
        "4. Выберите проект, затем отправляйте текст, файлы, скриншоты или команды Claude.\n\n"
        "<b>Команды:</b>\n"
        "/projects - выбрать проект\n"
        "/status - статус текущей сессии\n"
        "/connect - подключить свои сервисы (Notion, GitHub)\n"
        "/settings - настройки\n"
        "/stop - остановить текущую генерацию\n"
        "/close - закрыть сессию\n"
        "/auth - диагностика авторизации Claude Code"
    )


def auth_message(*, service_name: str = SERVICE_NAME) -> str:
    return (
        "<b>Claude Code authorization</b>\n\n"
        "The production installer checks Claude Code authorization before starting Vels Claude.\n\n"
        "If authorization breaks later, run Claude Code as the same Linux user that runs the service:\n"
        "<code>claude</code>\n"
        "<code>claude -p \"ping\" --output-format stream-json --verbose</code>\n\n"
        "VPS diagnostics:\n"
        f"<code>systemctl status {escape(service_name)}</code>\n"
        f"<code>journalctl -u {escape(service_name)} -n 50 --no-pager</code>"
    )


def new_session_prompt_message(*, auto_created: bool = False) -> str:
    title = "Новая сессия Vels Claude" if auto_created else "Новая сессия"
    lead = (
        "Топик создан автоматически. Следующий шаг - выбрать проект."
        if auto_created
        else "Выберите проект для работы."
    )
    return (
        f"<b>{title}</b>\n\n"
        f"{lead}\n\n"
        "После выбора проекта отправьте первую задачу, файл, скриншот или команду Claude."
    )


def no_projects_message(projects_dir: Path | str) -> str:
    projects_path = escape(str(projects_dir))
    return (
        "<b>Проекты не найдены</b>\n\n"
        f"Vels Claude сейчас ищет проекты здесь:\n<code>{projects_path}</code>\n\n"
        "Создайте или склонируйте папки проектов внутрь этой директории, затем запустите /projects еще раз."
    )


def project_ready_message(project_name: str, project_path: str) -> str:
    return (
        f"<b>Проект выбран:</b> {escape(project_name)}\n\n"
        f"Путь: <code>{escape(project_path)}</code>\n\n"
        "Сессия готова. Отправьте задачу текстом, файл, скриншот или slash-команду Claude Code.\n\n"
        "Полезные команды: /status, /settings, /stop, /close"
    )


def auto_topic_failure_message() -> str:
    return (
        "Не удалось создать топик автоматически.\n\n"
        "Проверьте в BotFather, что для бота включены Topics in private chats, "
        "и что у бота есть право создавать топики. Затем создайте новый топик вручную "
        "и отправьте сообщение там."
    )


def no_session_in_topic_message() -> str:
    return (
        "В этом топике пока нет сессии Vels Claude.\n"
        "Выберите проект, чтобы привязать топик к рабочей папке."
    )


def project_missing_message(project_path: str) -> str:
    return (
        "<b>Папка проекта больше не найдена</b>\n\n"
        f"Ожидался путь:\n<code>{escape(project_path)}</code>\n\n"
        "Проверьте, что проект существует на сервере, или выберите другой проект через /projects."
    )


def claude_error_message(error_type: str, raw_content: str) -> str:
    lower = raw_content.lower()
    if error_type == "cli_missing" or "not found" in lower or "no such file" in lower:
        return (
            "<b>Claude Code CLI не найден</b>\n\n"
            "Запустите /auth для диагностики. На VPS проверьте установку через production installer "
            "и логи сервиса:\n"
            f"<code>journalctl -u {escape(SERVICE_NAME)} -n 50 --no-pager</code>"
        )
    if error_type == "auth" or any(token in lower for token in ("unauthorized", "401", "403", "forbidden")):
        return (
            "<b>Claude Code не авторизован</b>\n\n"
            "Запустите /auth и проверьте, что команда "
            "<code>claude -p \"ping\" --output-format stream-json --verbose</code> "
            "успешно выполняется для service user."
        )
    if error_type == "context_overflow" or any(
        kw in lower for kw in ("context length", "context window", "token limit", "prompt is too long", "chunk is longer", "exceeds maximum")
    ):
        return (
            "<b>Контекст сессии переполнен</b>\n\n"
            "Попробуйте:\n"
            "- <code>/compact</code> - сжать историю\n"
            "- <code>/clear</code> - очистить историю\n"
            "- <code>/close</code> - начать новую сессию"
        )
    if error_type == "rate_limit" or any(
        kw in lower for kw in ("rate limit", "429", "too many requests")
    ):
        return "Превышен лимит запросов Claude. Подождите немного и попробуйте снова."
    if error_type in {"timeout", "stall"} or any(kw in lower for kw in ("timeout", "timed out")):
        return "Claude Code не завершил ответ вовремя. Попробуйте повторить запрос или используйте /stop."
    return f"Ошибка Claude Code: {escape(raw_content)}"
