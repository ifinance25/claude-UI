#!/usr/bin/env bash
#
# reset-admin-password.sh — восстановление доступа к ВЕБ-АДМИНКЕ, если
# оператор потерял пароль локального админа `ADMIN_LOGIN` (он показывается
# ровно один раз при установке и больше нигде не хранится в открытом виде).
#
# Пароль лежит в БД сессий как PBKDF2-хэш (src/web/passwords.py) —
# расшифровать его нельзя, поэтому скрипт ЗАДАЁТ НОВЫЙ пароль тому же
# локальному админу. Он применяется мгновенно: логин каждый раз сверяет
# пароль с БД, а инкремент token_version отзывает старые JWT-сессии.
#
# Использование:
#   sudo bash scripts/reset-admin-password.sh [новый-пароль]
#
#     • без аргумента — сгенерируется стойкий пароль и будет напечатан;
#     • INSTALL_DIR=/path sudo -E bash ... — переопределить каталог установки.
#
# Почему root + запуск python ОТ СЕРВИСНОГО пользователя: БД принадлежит
# сервис-юзеру (напр. `vels`), не root. Если запустить python под root, он
# создаст root-owned WAL/SHM рядом с sessions.db и сломает владение → сервис
# перестанет писать в БД. Поэтому оператор зовёт скрипт через sudo (root),
# а python исполняется под владельцем .venv (`runuser`/`sudo -u`).
#
set -euo pipefail

# ── 1. Каталог установки ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${INSTALL_DIR:-}" ]]; then
    INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
# Запасной вариант: скрипт запущен из необычного места, а рядом нет src/.
if [[ ! -d "$INSTALL_DIR/src" && -d /opt/vels-claude/src ]]; then
    INSTALL_DIR="/opt/vels-claude"
fi

# ── 2. Требуем root ──────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ОШИБКА: нужен root — запустите через sudo:" >&2
    echo "  sudo bash $0 [новый-пароль]" >&2
    exit 1
fi

# ── 3. Проверки окружения ────────────────────────────────────────────
if [[ ! -d "$INSTALL_DIR/src" ]]; then
    echo "ОШИБКА: не похоже на каталог установки Vels-Claude: $INSTALL_DIR" >&2
    echo "  Укажите его явно: INSTALL_DIR=/opt/vels-claude sudo -E bash $0" >&2
    exit 1
fi

VENV_PY="$INSTALL_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    echo "ОШИБКА: не найден venv-python: $VENV_PY" >&2
    echo "  Проверьте INSTALL_DIR (сейчас: $INSTALL_DIR) или переустановите бота." >&2
    exit 1
fi

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    echo "ПРЕДУПРЕЖДЕНИЕ: не найден $INSTALL_DIR/.env — логин админа будет взят" >&2
    echo "  из config.yaml или по умолчанию 'admin'." >&2
fi

# ── 4. Сервисный пользователь = владелец .venv ───────────────────────
SVC_USER="$(stat -c '%U' "$INSTALL_DIR/.venv")"
if [[ -z "$SVC_USER" || "$SVC_USER" == "UNKNOWN" ]]; then
    echo "ОШИБКА: не удалось определить сервисного пользователя" >&2
    echo "  (владельца $INSTALL_DIR/.venv через 'stat -c %U')." >&2
    exit 1
fi

# ── 5. Новый пароль (аргумент или генерируем стойкий) ────────────────
if [[ $# -ge 1 && -n "${1:-}" ]]; then
    NEW_PASSWORD="$1"
else
    NEW_PASSWORD="$("$VENV_PY" -c 'import secrets; print(secrets.token_urlsafe(15))')"
fi

# ── 6. Запуск python ОТ имени сервисного пользователя ────────────────
# Пароль передаём через STDIN (не argv и не env), чтобы он не светился в
# `ps`/`/proc/<pid>/cmdline`. CWD наследуется дочерним процессом (и runuser,
# и sudo сохраняют текущий каталог), поэтому относительный путь к БД и импорт
# пакета `src` резолвятся так же, как у работающего сервиса.
cd "$INSTALL_DIR"

run_as_svc() {
    # runuser предпочтительнее под root (без политик sudoers); sudo -u — фолбэк.
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$SVC_USER" -- "$@"
    else
        sudo -u "$SVC_USER" -- "$@"
    fi
}

# Python-код читается через heredoc, чтобы избежать ада экранирования.
# `read -d ''` возвращает 1 на EOF — гасим через `|| true` под set -e.
read -r -d '' PYCODE <<'PYEOF' || true
import os
import sys
from pathlib import Path

# CWD наследован = каталог установки; фиксируем на всякий случай и кладём в
# sys.path, чтобы `import src.*` работал независимо от политики chdir.
install_dir = os.getcwd()
sys.path.insert(0, install_dir)

from src.config import Settings
from src.claude.session import SessionManager
from src.web.passwords import hash_password

new_pass = sys.stdin.read()
if not new_pass:
    print("ОШИБКА: получен пустой пароль", file=sys.stderr)
    sys.exit(2)

# Грузим настройки ТОЧНО как приложение (src/main.py): from_yaml сам мёржит
# config/config.local.yaml поверх config/config.yaml и читает .env.
cfg = Path("config/config.yaml")
if not cfg.exists():
    cfg = Path("../config/config.yaml")
settings = Settings.from_yaml(cfg)

login = settings.get_admin_login() or "admin"
db_path = settings.get_session_database_path()

sm = SessionManager(storage_path=db_path)
try:
    user = sm.get_user_by_username(login)
    if user is None:
        print(
            f"ОШИБКА: нет локального админа '{login}' — задайте "
            "ADMIN_LOGIN/ADMIN_PASSWORD в .env и переустановите, "
            "или войдите через /weblogin",
            file=sys.stderr,
        )
        sys.exit(1)

    ok = sm.set_user_password(user["user_id"], hash_password(new_pass))
    if not ok:
        print("ОШИБКА: не удалось обновить пароль (пользователь исчез?)", file=sys.stderr)
        sys.exit(1)

    bar = "=" * 56
    print(bar)
    print("  Пароль веб-админа СБРОШЕН. СОХРАНИТЕ эти данные:")
    print(bar)
    print(f"  Логин:  {login}")
    print(f"  Пароль: {new_pass}")
    print(bar)
    print(f"  БД:     {db_path}")
    print("  Действует СРАЗУ (логин сверяет пароль с БД при каждом входе).")
    print("  Прежние веб-сессии этого админа отозваны (token_version++).")
    print(bar)
finally:
    try:
        sm.close_sync()
        sm._engine.sync_engine.dispose()
    except Exception:
        pass
PYEOF

echo "Каталог установки: $INSTALL_DIR"
echo "Сервисный юзер:    $SVC_USER"
echo "Сбрасываю пароль веб-админа..."

if printf '%s' "$NEW_PASSWORD" | run_as_svc "$VENV_PY" -c "$PYCODE"; then
    exit 0
else
    rc=$?
    echo "" >&2
    echo "Сброс НЕ выполнен (см. сообщение выше)." >&2
    exit "$rc"
fi
