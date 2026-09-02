#!/usr/bin/env bash
# uninstall.sh - Vels Claude uninstaller for Ubuntu/Debian VPS.
#
# Удаляет ТОЛЬКО артефакты установщика:
#   - systemd-сервис vels-claude
#   - директорию /opt/vels-claude (или INSTALL_DIR)
#   - service user vels-bot (только если был создан установщиком)
#
# НЕ ТРОГАЕТ:
#   - PROJECTS_DIR (там код пользователя!)
#   - ~/.claude (настройки Claude Code)
#   - Claude Code CLI (npm -g @anthropic-ai/claude-code)
#   - Node.js, Python, apt-пакеты
#
# Безопасно для повторного запуска.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-}"
SERVICE_NAME="${SERVICE_NAME:-vels-claude}"
UNIT_PATH="${UNIT_PATH:-/etc/systemd/system/${SERVICE_NAME}.service}"
VELS_BOT_USER="${VELS_BOT_USER:-vels-bot}"
ASSUME_YES="${ASSUME_YES:-0}"
KEEP_USER="${KEEP_USER:-0}"
NGINX_SITE_AVAIL="${NGINX_SITE_AVAIL:-/etc/nginx/sites-available/vels-web.conf}"
NGINX_SITE_ENABLED="${NGINX_SITE_ENABLED:-/etc/nginx/sites-enabled/vels-web.conf}"

C_RESET=$'\033[0m'
C_BOLD=$'\033[1m'
C_OK=$'\033[0;32m'
C_WARN=$'\033[0;33m'
C_ERR=$'\033[0;31m'
C_INFO=$'\033[0;34m'

log_info() { printf '%s[INFO]%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
log_ok()   { printf '%s[OK]%s %s\n'   "$C_OK"   "$C_RESET" "$*"; }
log_warn() { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
log_err()  { printf '%s[ERROR]%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; }
step()     { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
die()      { log_err "$*"; exit 1; }

usage() {
    cat <<EOF
Vels Claude uninstaller

Usage:
  curl -sSL https://agent.nickvels.ru/uninstall.sh | sudo bash
  sudo bash scripts/uninstall.sh [--yes] [--keep-user]

Options:
  -y, --yes        Пропустить интерактивное подтверждение
      --keep-user  Не удалять vels-bot system user
  -h, --help       Эта справка

Env-переопределения:
  INSTALL_DIR    Путь установки (default: auto-detect среди /opt/vels-claude и др.)
  SERVICE_NAME   Имя systemd-сервиса (default: vels-claude)

Что удаляется:
  - systemd service: \$SERVICE_NAME
  - директория установки
  - system user vels-bot (только если был создан установщиком)

Что НЕ удаляется:
  - папка проектов (PROJECTS_DIR из .env) — если она внутри home сервис-юзера,
    home тоже сохраняется целиком
  - ~/.claude (настройки Claude Code того, кто запускает)
  - Claude Code CLI, Node.js, Python, apt-пакеты
EOF
}

parse_args() {
    while (($#)); do
        case "$1" in
            -y|--yes) ASSUME_YES=1 ;;
            --keep-user) KEEP_USER=1 ;;
            -h|--help) usage; exit 0 ;;
            *) die "Unknown option: $1 (try --help)" ;;
        esac
        shift
    done
}

ensure_root() {
    [[ $EUID -eq 0 ]] || die "Run through sudo: curl -sSL https://agent.nickvels.ru/uninstall.sh | sudo bash"
}

# Авто-поиск директории установки в стандартных локациях.
detect_install_dir() {
    if [[ -n "$INSTALL_DIR" ]]; then
        return 0
    fi
    local candidates=(
        "/opt/vels-claude"
        "/srv/vels-claude"
        "/opt/telegram-claude-code"
        "/srv/telegram-claude-code"
    )
    local dir
    for dir in "${candidates[@]}"; do
        if [[ -f "$dir/src/main.py" && -f "$dir/scripts/install.sh" ]]; then
            INSTALL_DIR="$dir"
            return 0
        fi
    done
    # Если unit-файл есть, попробуем достать WorkingDirectory из него.
    if [[ -f "$UNIT_PATH" ]]; then
        local wd
        wd="$(grep -E '^WorkingDirectory=' "$UNIT_PATH" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
        if [[ -n "$wd" && -f "$wd/src/main.py" ]]; then
            INSTALL_DIR="$wd"
            return 0
        fi
    fi
    return 1
}

# Главный предохранитель: убедиться, что директория действительно
# принадлежит Vels Claude. Любая ошибка — отказ удалять.
verify_install_dir() {
    local dir=$1
    [[ -n "$dir" ]] || return 1
    [[ "$dir" == /* ]] || return 1
    [[ "$dir" != "/" ]] || return 1
    case "$dir" in
        /usr|/usr/*|/etc|/etc/*|/home|/home/*|/root|/var|/var/lib|/bin|/sbin|/lib|/lib64|/boot|/dev|/proc|/sys|/run|/tmp)
            return 1
            ;;
    esac
    [[ -f "$dir/src/main.py" ]] || return 1
    [[ -f "$dir/scripts/install.sh" ]] || return 1
    grep -q "Vels Claude" "$dir/scripts/install.sh" 2>/dev/null || return 1
    return 0
}

# Достать User= из systemd unit, чтобы знать, кого удалять (если можно).
detect_service_user() {
    [[ -f "$UNIT_PATH" ]] || return 1
    local user
    user="$(grep -E '^User=' "$UNIT_PATH" | head -1 | cut -d= -f2- | tr -d '[:space:]')"
    [[ -n "$user" ]] || return 1
    printf '%s' "$user"
}

# Достать PROJECTS_DIR из .env, чтобы показать пользователю и точно НЕ удалять.
service_home_of() {
    local user=$1
    [[ -n "$user" ]] || return 1
    getent passwd "$user" | cut -d: -f6
}

# Дефолтный PROJECTS_DIR (/var/lib/vels-bot/projects) лежит ВНУТРИ home
# сервис-юзера, а `userdel -r` сносит home целиком — вместе с кодом, который
# человек писал через Vels Claude. Проверка ниже по строке 196 ловила только
# случай «проекты внутри INSTALL_DIR» и этот, основной, пропускала: на живом
# сервере файл-маркер в projects удалялся, а экран при этом печатал, что папка
# проектов сохраняется.
projects_inside_home() {
    local projects=$1 home=$2
    [[ -n "$projects" && -n "$home" && "$home" != "/" ]] || return 1
    [[ "$projects" == "$home" || "$projects" == "$home"/* ]]
}

detect_projects_dir() {
    local env_file="${INSTALL_DIR}/.env"
    [[ -f "$env_file" ]] || return 1
    local val
    val="$(grep -E '^PROJECTS_DIR=' "$env_file" | head -1 | cut -d= -f2-)"
    [[ -n "$val" ]] || return 1
    printf '%s' "$val"
}

# Был ли user создан НАШИМ установщиком? Признак — GECOS comment.
is_managed_user() {
    local user=$1
    [[ "$user" == "$VELS_BOT_USER" ]] || return 1
    local entry comment
    entry="$(getent passwd "$user" 2>/dev/null || true)"
    [[ -n "$entry" ]] || return 1
    comment="$(printf '%s' "$entry" | cut -d: -f5)"
    [[ "$comment" == "Vels Claude service account" ]]
}

confirm() {
    (( ASSUME_YES == 1 )) && return 0
    local prompt=$1
    local reply=""
    if [[ -t 0 ]]; then
        read -r -p "$prompt [y/N]: " reply
    elif [[ -r /dev/tty ]]; then
        read -r -p "$prompt [y/N]: " reply </dev/tty
    else
        die "Нет интерактивного терминала — добавьте --yes для авто-подтверждения."
    fi
    [[ "$reply" =~ ^[yY]([eE][sS])?$ ]]
}

main() {
    parse_args "$@"
    ensure_root

    step "Vels Claude — деинсталляция"

    detect_install_dir || true

    local install_dir_present=0
    local service_user=""
    local projects_dir=""

    if [[ -n "$INSTALL_DIR" ]]; then
        if verify_install_dir "$INSTALL_DIR"; then
            install_dir_present=1
            projects_dir="$(detect_projects_dir 2>/dev/null || true)"
            # Защита: PROJECTS_DIR не должен лежать внутри INSTALL_DIR,
            # иначе rm -rf снесёт пользовательские проекты вместе с ботом.
            if [[ -n "$projects_dir" && "$projects_dir" == "$INSTALL_DIR"* ]]; then
                die "PROJECTS_DIR ($projects_dir) лежит ВНУТРИ INSTALL_DIR ($INSTALL_DIR). Удаление снесло бы ваши проекты — отказ. Перенесите проекты вне $INSTALL_DIR и запустите снова."
            fi
        else
            die "$INSTALL_DIR не похож на установку Vels Claude (нет маркеров). Удаление отменено."
        fi
    fi

    if [[ -f "$UNIT_PATH" ]]; then
        service_user="$(detect_service_user 2>/dev/null || true)"
    fi

    # ---------- План ----------
    step "Будет удалено"
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        printf '  - systemd-сервис:     %s (запущен, будет остановлен)\n' "$SERVICE_NAME"
    elif [[ -f "$UNIT_PATH" ]]; then
        printf '  - systemd-сервис:     %s (не запущен)\n' "$SERVICE_NAME"
    else
        printf '  - systemd-сервис:     не найден (пропуск)\n'
    fi
    if [[ -f "$UNIT_PATH" ]]; then
        printf '  - unit-файл:          %s\n' "$UNIT_PATH"
    fi
    if (( install_dir_present == 1 )); then
        printf '  - директория:         %s\n' "$INSTALL_DIR"
    elif [[ -n "$INSTALL_DIR" ]]; then
        printf '  - директория:         %s (не найдена, пропуск)\n' "$INSTALL_DIR"
    else
        printf '  - директория:         не обнаружена (пропуск)\n'
    fi
    if (( KEEP_USER == 0 )) && [[ -n "$service_user" ]] && is_managed_user "$service_user"; then
        local home_dir
        home_dir="$(service_home_of "$service_user" || true)"
        if projects_inside_home "$projects_dir" "$home_dir"; then
            printf '  - system user:        %s (БЕЗ home: в %s лежит папка проектов)\n' \
                "$service_user" "$home_dir"
        else
            printf '  - system user:        %s (с home %s)\n' "$service_user" "$home_dir"
        fi
    fi
    if [[ -f "$NGINX_SITE_AVAIL" ]] || [[ -L "$NGINX_SITE_ENABLED" ]]; then
        printf '  - nginx site:         %s\n' "$NGINX_SITE_AVAIL"
        if [[ -L "$NGINX_SITE_ENABLED" ]]; then
            printf '  - nginx symlink:      %s\n' "$NGINX_SITE_ENABLED"
        fi
    fi

    step "НЕ будет удалено (сохраняется)"
    if [[ -n "$projects_dir" ]]; then
        printf '  - папка проектов:     %s\n' "$projects_dir"
    fi
    local home_dir
    home_dir="$(service_home_of "$service_user" || true)"
    if projects_inside_home "$projects_dir" "$home_dir"; then
        printf '  - home сервис-юзера:  %s (сохраняется целиком — внутри проекты)\n' "$home_dir"
    elif [[ -n "$home_dir" ]] && (( KEEP_USER == 0 )); then
        # Авторизация Claude под сервис-юзером (вариант B в install.sh) живёт
        # именно здесь — вместе с home она исчезнет, и это надо сказать вслух.
        printf '  - ВНИМАНИЕ: %s/.claude удалится вместе с home сервис-юзера\n' "$home_dir"
    fi
    printf '  - ~/.claude:          настройки Claude Code того, кто запускает\n'
    printf '  - Claude Code CLI:    @anthropic-ai/claude-code (npm -g)\n'
    printf '  - Node.js, Python, apt-пакеты\n'
    printf '  - Let'\''s Encrypt:    /etc/letsencrypt (сертификаты сохраняются)\n'
    printf '  - Caddy:              сам веб-сервер остаётся (удаляется только наш конфиг)\n'
    if [[ -n "$service_user" ]] && ! is_managed_user "$service_user"; then
        printf '  - user %s:        не удаляется (создан не установщиком)\n' "$service_user"
    fi
    if (( KEEP_USER == 1 )) && [[ -n "$service_user" ]] && is_managed_user "$service_user"; then
        printf '  - user %s:        --keep-user, оставлен\n' "$service_user"
    fi

    echo
    confirm "Продолжить удаление?" || { log_info "Отменено."; exit 0; }

    # ---------- Выполнение ----------
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        step "Останавливаю сервис"
        systemctl stop "$SERVICE_NAME" || log_warn "systemctl stop $SERVICE_NAME упал (продолжаю)"
        log_ok "Сервис остановлен"
    fi

    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        systemctl disable "$SERVICE_NAME" 2>/dev/null || true
        log_ok "Сервис отключён из автозапуска"
    fi

    if [[ -f "$UNIT_PATH" ]]; then
        step "Удаляю unit-файл"
        rm -f -- "$UNIT_PATH"
        systemctl daemon-reload
        log_ok "Удалён $UNIT_PATH"
    fi

    if [[ -L "$NGINX_SITE_ENABLED" ]] || [[ -f "$NGINX_SITE_AVAIL" ]]; then
        step "Удаляю nginx site"
        rm -f -- "$NGINX_SITE_ENABLED" "$NGINX_SITE_AVAIL"
        if command -v nginx >/dev/null 2>&1 && nginx -t 2>/dev/null; then
            systemctl reload nginx 2>/dev/null || log_warn "systemctl reload nginx упал (продолжаю)"
            log_ok "nginx site удалён, nginx перезагружен"
        else
            log_warn "nginx -t упал или nginx не найден — site файлы удалены, перезагрузка пропущена"
        fi
    fi

    if (( install_dir_present == 1 )); then
        step "Удаляю директорию установки"
        # Параноидальная повторная проверка перед rm -rf.
        verify_install_dir "$INSTALL_DIR" || die "Финальная проверка не пройдена для $INSTALL_DIR — отмена."
        rm -rf -- "$INSTALL_DIR"
        log_ok "Удалена $INSTALL_DIR"
    fi

    if (( KEEP_USER == 0 )) && [[ -n "$service_user" ]] && is_managed_user "$service_user"; then
        step "Удаляю system user"
        local home_dir keep_home=0
        home_dir="$(service_home_of "$service_user" || true)"
        if projects_inside_home "$projects_dir" "$home_dir"; then
            keep_home=1
        fi
        if (( keep_home == 1 )); then
            # Без -r: home остаётся вместе с папкой проектов. Потерять чужой код
            # молча хуже, чем оставить каталог, который человек удалит сам.
            if userdel "$service_user" 2>/dev/null; then
                log_ok "Удалён user $service_user (home $home_dir сохранён — внутри проекты)"
            else
                log_warn "userdel $service_user упал (возможно, остались процессы). Попробуйте вручную."
            fi
            log_info "Папка проектов осталась: $projects_dir"
            log_info "Если она больше не нужна — удалите вручную: rm -rf $home_dir"
        elif userdel -r "$service_user" 2>/dev/null; then
            log_ok "Удалён user $service_user"
        else
            log_warn "userdel -r $service_user упал (возможно, остались процессы). Попробуйте вручную."
        fi
    fi

    step "Готово"
    cat <<EOF

Vels Claude удалён.

Сохранено (удалить вручную при необходимости):
EOF
    [[ -n "$projects_dir" ]] && printf '  - Папка проектов:     %s\n' "$projects_dir"
    cat <<'EOF'
  - Claude Code CLI:    sudo npm uninstall -g @anthropic-ai/claude-code
  - Claude settings:    rm -rf ~/.claude

EOF
}

(return 0 2>/dev/null) || main "$@"
