"""`vels` — небольшая CLI к Vels Claude.

Главное назначение: после закрытия терминала легко узнать ТЕКУЩИЙ адрес веб-
платформы и статус сервиса, не роясь в конфигах и логах.

    vels url       — публичный адрес веб-платформы (и ссылка для входа)
    vels status    — адрес, режим веба и статус systemd-сервиса
    vels open      — то же, что url, но печатает только URL (для скриптов)
    vels --help    — список команд

Конфиг читается так же, как у приложения (config/config.yaml + config.local.yaml),
сам бот при этом НЕ запускается.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Имя systemd-сервиса (см. scripts/*.service). Переопределяется --service.
DEFAULT_SERVICE = "vels-claude"
# Корень репозитория = родитель каталога src/.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.yaml"


def _load_settings(config_path: str):
    # Импорт внутри функции — чтобы `vels --help` работал даже при кривом
    # окружении/зависимостях.
    from src.config.settings import get_settings

    return get_settings(config_path)


def _web_base(settings) -> tuple[str | None, str, bool]:
    """(public_url | None, local_url, web_enabled)."""
    web = settings.web
    public = (web.public_origin or "").strip().rstrip("/") or None
    local = f"http://{web.host}:{web.port}"
    return public, local, bool(web.enabled)


def _effective_url(settings) -> str:
    public, local, _enabled = _web_base(settings)
    return public or local


def _service_status(service: str) -> str:
    """Краткий статус systemd-сервиса (best-effort, кроссплатформенно)."""
    if not shutil.which("systemctl"):
        return "n/a (нет systemctl — не Linux/systemd)"
    try:
        active = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        enabled = subprocess.run(
            ["systemctl", "is-enabled", service],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        return f"{active or 'unknown'} (автозапуск: {enabled or 'unknown'})"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _cmd_url(settings, *, quiet: bool) -> int:
    public, local, enabled = _web_base(settings)
    url = public or local
    if quiet:
        print(url)
        return 0
    if not enabled:
        print("Веб-интерфейс выключен (web.enabled: false в config.yaml).")
        print(f"Если включить — слушал бы локально: {local}")
        return 0
    print(f"Адрес веб-платформы: {url}")
    if public:
        print(f"Вход (Telegram magic-link): {public}/login")
    else:
        print(f"(локальный bind {local} — для доступа извне нужен nginx или")
        print(" публичный домен; задайте web.public_origin)")
    return 0


def _cmd_status(settings, service: str) -> int:
    public, local, enabled = _web_base(settings)
    print("Vels Claude — статус")
    print(f"  Веб включён:     {'да' if enabled else 'нет'}")
    print(f"  Адрес:           {public or local}")
    print(f"  Локальный bind:  {local}")
    if public:
        print(f"  Публичный URL:   {public}")
        print(f"  Вход:            {public}/login")
    print(f"  Сервис {service}: {_service_status(service)}")
    print(f"  Логи:            journalctl -u {service} -f")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vels",
        description="CLI к Vels Claude: адрес веб-платформы и статус.",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=f"путь к config.yaml (по умолчанию {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help=f"имя systemd-сервиса (по умолчанию {DEFAULT_SERVICE})",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("url", help="адрес веб-платформы и ссылка для входа")
    sub.add_parser("open", help="только URL (для скриптов)")
    sub.add_parser("status", help="адрес, режим веба и статус сервиса")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "url"  # без аргументов — показываем адрес

    try:
        settings = _load_settings(args.config)
    except Exception as exc:  # noqa: BLE001 — дружелюбное сообщение вместо трейсбэка
        print(f"Не удалось прочитать конфиг ({args.config}): {exc}", file=sys.stderr)
        return 2

    if command == "open":
        return _cmd_url(settings, quiet=True)
    if command == "status":
        return _cmd_status(settings, args.service)
    return _cmd_url(settings, quiet=False)


if __name__ == "__main__":
    raise SystemExit(main())
