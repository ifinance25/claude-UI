#!/usr/bin/env bash
# install.sh - AI-Panel production installer for Ubuntu/Debian VPS.
# Веб-интерфейс ставится всегда; Telegram-бот — необязательный шаг (Enter пропускает).
# Safe to re-run. Sourceable by tests; main runs only when executed.

set -euo pipefail

# Ubuntu 22.04+ ставит needrestart, и тот после каждой установки пакетов
# открывает диалог «Which services should be restarted?» и ЖДЁТ ввода.
# Установщик приходит к человеку бутстрапом (`curl … | sudo bash`), где stdin —
# пайп от curl: отвечать диалогу некому, установка виснет насмерть. Свои
# вопросы мы задаём явно через </dev/tty, поэтому автоматический режим здесь
# ничего не ломает. Оба флага экспортируем: apt дёргают и вложенные скрипты
# (setup_20.x от NodeSource), их окружение тоже должно быть неинтерактивным.
export NEEDRESTART_MODE=a
export DEBIAN_FRONTEND=noninteractive

INSTALL_DIR="${INSTALL_DIR:-/opt/vels-claude}"
GH_TOKEN="${GH_TOKEN:-}"
REPO_OWNER="${REPO_OWNER:-ifinance25}"
REPO_NAME="${REPO_NAME:-claude-UI}"
if [[ -n "$GH_TOKEN" ]]; then
    REPO_URL="${REPO_URL:-https://oauth2:${GH_TOKEN}@github.com/${REPO_OWNER}/${REPO_NAME}.git}"
else
    REPO_URL="${REPO_URL:-https://github.com/${REPO_OWNER}/${REPO_NAME}.git}"
fi
REPO_BRANCH="${REPO_BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-vels-claude}"
UNIT_PATH="${UNIT_PATH:-/etc/systemd/system/${SERVICE_NAME}.service}"
VELS_BOT_USER="${VELS_BOT_USER:-vels-bot}"
VELS_BOT_HOME="${VELS_BOT_HOME:-/var/lib/${VELS_BOT_USER}}"

C_RESET=$'\033[0m'
C_BOLD=$'\033[1m'
C_OK=$'\033[0;32m'
C_WARN=$'\033[0;33m'
C_ERR=$'\033[0;31m'
C_INFO=$'\033[0;34m'

SUDO=""
SERVICE_USER=""
SERVICE_HOME=""
SERVICE_GROUP=""
SERVICE_NEEDS_CREATE=0
CFG_TOKEN=""
CFG_IDS=""
CFG_OWNER_USER_ID=""
CFG_PROJECTS_DIR=""
CFG_BOT_USERNAME=""
CFG_ADMIN_LOGIN=""
CFG_ADMIN_PASSWORD=""
CFG_ADMIN_PASSWORD_GENERATED=0
CFG_JWT_SECRET=""
CFG_CONNECTIONS_KEY=""
CFG_PUBLIC_ORIGIN=""
CFG_WEB_HOST="127.0.0.1"
CFG_WEB_PORT="8765"
CLAUDE_BIN_PATH=""
CFG_ANTHROPIC_API_KEY=""
CFG_CLAUDE_OAUTH_TOKEN=""
CLAUDE_AUTH_STATUS=""
CFG_WEB_BUILT=0
CFG_WEB_REACHABLE=0
CFG_EXTERNAL_REACHABLE=0
CFG_DOMAIN=""
CFG_PERSIST_DOMAIN=""
CFG_WEB_MODE="ip"
CFG_VITE_BASE="/agent/"

log_info() { printf '%s[INFO]%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
log_ok() { printf '%s[OK]%s %s\n' "$C_OK" "$C_RESET" "$*"; }
log_warn() { printf '%s[WARN]%s %s\n' "$C_WARN" "$C_RESET" "$*"; }
log_err() { printf '%s[ERROR]%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; }
step() { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }
die() { log_err "$*"; exit 1; }

# H-2: pick_python/ensure_python311 живут в общем sourceable-хелпере (нужны и
# update.sh — пересоздание legacy-venv при обновлении). Источник истины —
# scripts/lib/python-env.sh рядом с этим файлом; log_warn/die выше уже
# определены, поэтому хелпер использует их (guard внутри — no-op).
# Раздача одним файлом без соседей (см. README «Для разработчиков» — прямой
# curl сырого install.sh) — в этом случае хелпера ещё нет на диске; он
# подхватывается лениво внутри install_python_env() из уже склонированного
# $INSTALL_DIR (install_or_update_repo к тому моменту уже отработал).
_VELS_INSTALL_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
if [[ -n "$_VELS_INSTALL_SH_DIR" && -f "$_VELS_INSTALL_SH_DIR/lib/python-env.sh" ]]; then
    # shellcheck source=./lib/python-env.sh
    source "$_VELS_INSTALL_SH_DIR/lib/python-env.sh"
fi
unset _VELS_INSTALL_SH_DIR

print_banner() {
    cat <<'EOF'

AI-Panel - production installer
Full Telegram Claude Code bot for Ubuntu/Debian VPS

EOF
}

validate_token_format() {
    local token=${1:-}
    [[ -n "$token" ]] || return 1
    [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || return 1
    printf 'ok'
}

sanitize_input() {
    # Чистит вставленное в терминал значение от артефактов вставки. Без этого
    # живой ввод токена/ID подставлял пользователей: терминал в режиме bracketed
    # paste оборачивает вставку в ESC[200~..ESC[201~, Windows-буфер тащит \r, а
    # лишние пробелы ломают строгую проверку формата. В итоге человек вставлял
    # верный токен и видел то «неверный формат», то зависание на вводе.
    local s=${1:-}
    local esc=$'\033'
    # Маркеры bracketed paste (целиком, в любом месте строки).
    s=${s//"${esc}[200~"/}
    s=${s//"${esc}[201~"/}
    # Перевод строки/возврат каретки/таб (CRLF из чужого буфера, случайный таб).
    s=${s//$'\r'/}
    s=${s//$'\n'/}
    s=${s//$'\t'/}
    # Любой оставшийся управляющий ESC-символ.
    s=${s//"$esc"/}
    # Срезать крайние пробелы (внутренние не трогаем — их отбракует валидатор).
    s=${s#"${s%%[![:space:]]*}"}
    s=${s%"${s##*[![:space:]]}"}
    printf '%s' "$s"
}

parse_user_ids() {
    local raw=${1:-}
    [[ -n "$raw" ]] || return 1
    [[ "$raw" != *$'\n'* && "$raw" != *$'\r'* ]] || return 1
    local remaining="${raw},"
    local out=()
    local seen=","
    local part
    while [[ -n "$remaining" ]]; do
        part="${remaining%%,*}"
        remaining="${remaining#*,}"
        part="${part#"${part%%[![:space:]]*}"}"
        part="${part%"${part##*[![:space:]]}"}"
        [[ "$part" =~ ^[0-9]+$ ]] || return 1
        if [[ "$seen" != *",$part,"* ]]; then
            out+=("$part")
            seen="${seen}${part},"
        fi
    done
    ((${#out[@]} > 0)) || return 1
    (IFS=,; printf '%s' "${out[*]}")
}

expand_absolute_path() {
    local raw=${1:-}
    [[ -n "$raw" ]] || return 1
    local home_base="${2:-${SERVICE_HOME:-${HOME:-}}}"
    local result
    if [[ "$raw" == "~" ]]; then
        [[ -n "$home_base" ]] || return 1
        result="$home_base"
    elif [[ "$raw" == "~/"* ]]; then
        [[ -n "$home_base" ]] || return 1
        result="$home_base/${raw#"~/"}"
    elif [[ "$raw" == "/"* ]]; then
        result="$raw"
    else
        return 1
    fi
    [[ "$result" != "/" && "$result" == */ ]] && result="${result%/}"
    printf '%s' "$result"
}

passwd_entry_for_user() {
    local username=$1
    local entry
    if [[ "${VELS_PASSWD_ENTRY_OVERRIDE+x}" == x ]]; then
        while IFS= read -r entry; do
            if [[ "$entry" == "$username:"* ]]; then
                printf '%s' "$entry"
                return 0
            fi
        done <<<"$VELS_PASSWD_ENTRY_OVERRIDE"
        return 0
    fi
    getent passwd "$username" 2>/dev/null || true
}

group_entry_for_gid() {
    local gid=$1
    local entry group_name group_gid
    if [[ "${VELS_GROUP_ENTRY_OVERRIDE+x}" == x ]]; then
        while IFS= read -r entry; do
            IFS=: read -r group_name _ group_gid _ <<<"$entry"
            if [[ "$group_gid" == "$gid" ]]; then
                printf '%s' "$entry"
                return 0
            fi
        done <<<"$VELS_GROUP_ENTRY_OVERRIDE"
        return 0
    fi
    getent group "$gid" 2>/dev/null || true
}

home_for_user() {
    local username=$1
    local entry
    entry="$(passwd_entry_for_user "$username")"
    [[ -n "$entry" ]] || return 0
    printf '%s' "$entry" | cut -d: -f6
}

primary_group_for_user() {
    local username=$1
    local gid group group_entry passwd_entry
    passwd_entry="$(passwd_entry_for_user "$username")"
    gid="$(printf '%s' "$passwd_entry" | cut -d: -f4 || true)"
    if [[ -n "$gid" ]]; then
        group_entry="$(group_entry_for_gid "$gid")"
        group="$(printf '%s' "$group_entry" | cut -d: -f1 || true)"
    fi
    printf '%s' "${group:-$username}"
}

existing_unit_service_user() {
    # M-2: если юнит УЖЕ установлен (повторный запуск install.sh на живом
    # хосте), его User= — источник истины поверх дефолта/SUDO_USER ниже:
    # переключение сервис-юзера на re-run сломало бы владение data/.venv/HOME
    # относительно уже запущенного сервиса. VELS_UNIT_CONTENT_OVERRIDE — для
    # тестов (тот же приём, что VELS_PASSWD_ENTRY_OVERRIDE выше).
    if [[ "${VELS_UNIT_CONTENT_OVERRIDE+x}" == x ]]; then
        awk -F= '$1=="User"{print $2; exit}' <<<"$VELS_UNIT_CONTENT_OVERRIDE"
        return 0
    fi
    [[ -r "$UNIT_PATH" ]] || return 0
    awk -F= '$1=="User"{print $2; exit}' "$UNIT_PATH" 2>/dev/null
}

resolve_service_user() {
    local euid="${VELS_EUID_OVERRIDE:-$EUID}"
    if (( euid == 0 )); then
        # Приоритет 1: уже установленный юнит — НЕ переключаем сервис-юзера.
        local existing_user
        existing_user="$(existing_unit_service_user)"
        if [[ -n "$existing_user" ]]; then
            SERVICE_USER="$existing_user"
            SERVICE_HOME="$(home_for_user "$SERVICE_USER")"
            [[ -n "$SERVICE_HOME" ]] || SERVICE_HOME="/home/$SERVICE_USER"
            SERVICE_GROUP="$(primary_group_for_user "$SERVICE_USER")"
            SERVICE_NEEDS_CREATE=0
            return 0
        fi

        # Приоритет 2 (opt-in, ТОЛЬКО новые установки): явно попросили
        # переиспользовать личный логин-аккаунт (старое поведение).
        if [[ "${VELS_USE_LOGIN_USER:-}" == "1" \
              && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
            SERVICE_USER="$SUDO_USER"
            SERVICE_HOME="$(home_for_user "$SUDO_USER")"
            [[ -n "$SERVICE_HOME" ]] || SERVICE_HOME="/home/$SUDO_USER"
            SERVICE_GROUP="$(primary_group_for_user "$SERVICE_USER")"
            SERVICE_NEEDS_CREATE=0
            return 0
        fi

        # Дефолт (новые установки): выделенный системный аккаунт (nologin, без
        # sudo) — ДАЖЕ под sudo личного логина. На облачных VPS
        # (AWS/GCP/Azure/Oracle) личный логин обычно имеет NOPASSWD sudo;
        # отдавать его боту с bypassPermissions и ReadWritePaths=$HOME
        # расширяет поверхность атаки на весь /home/<user>. Старое поведение —
        # VELS_USE_LOGIN_USER=1 (см. выше). Уже авторизованная Claude-сессия
        # инвокера переносится отдельно, см. migrate_invoker_claude_session().
        local managed_entry
        managed_entry="$(passwd_entry_for_user "$VELS_BOT_USER")"
        SERVICE_USER="$VELS_BOT_USER"
        if [[ -n "$managed_entry" ]]; then
            SERVICE_HOME="$(home_for_user "$SERVICE_USER")"
            [[ -n "$SERVICE_HOME" ]] || SERVICE_HOME="$VELS_BOT_HOME"
            SERVICE_GROUP="$(primary_group_for_user "$SERVICE_USER")"
            SERVICE_NEEDS_CREATE=0
        else
            SERVICE_HOME="$VELS_BOT_HOME"
            SERVICE_GROUP="$VELS_BOT_USER"
            SERVICE_NEEDS_CREATE=1
        fi
        return 0
    fi

    die "Root privileges are required. Run: curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo bash"
}

migrate_invoker_claude_session() {
    # M-2: дефолт теперь — выделенный $VELS_BOT_USER вместо личного логина
    # инвокера. Чтобы не терять уже авторизованную Claude-сессию (SUDO_USER
    # мог раньше пройти `claude login` под своим аккаунтом), переносим его
    # ~/.claude в HOME сервис-юзера ОДИН РАЗ — только если у сервис-юзера
    # своей сессии ещё нет и мы реально на дефолтном (не opt-in login-user) пути.
    [[ "$SERVICE_USER" == "$VELS_BOT_USER" ]] || return 0
    [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" && "$SUDO_USER" != "$SERVICE_USER" ]] || return 0
    [[ -n "$SERVICE_HOME" ]] || return 0
    [[ -e "$SERVICE_HOME/.claude" ]] && return 0

    local invoker_home
    invoker_home="$(home_for_user "$SUDO_USER")"
    [[ -n "$invoker_home" ]] || invoker_home="/home/$SUDO_USER"
    [[ -d "$invoker_home/.claude" ]] || return 0

    if cp -a "$invoker_home/.claude" "$SERVICE_HOME/.claude" 2>/dev/null; then
        chown -R "$SERVICE_USER":"${SERVICE_GROUP:-$SERVICE_USER}" "$SERVICE_HOME/.claude"
        log_ok "Перенесена Claude-сессия $SUDO_USER -> $SERVICE_USER ($SERVICE_HOME/.claude)"
    else
        log_warn "Не удалось перенести ~/.claude из $invoker_home в $SERVICE_HOME — авторизуйте $SERVICE_USER вручную."
    fi
}

reconcile_projects_dir_with_user() {
    [[ "$SERVICE_USER" == "$VELS_BOT_USER" ]] || return 0
    if [[ "$CFG_PROJECTS_DIR" == "/root/projects" || "$CFG_PROJECTS_DIR" == "/root/"* ]]; then
        log_warn "Projects directory moved from $CFG_PROJECTS_DIR to $SERVICE_HOME/projects for $SERVICE_USER"
        CFG_PROJECTS_DIR="$SERVICE_HOME/projects"
    fi
}

render_env_file() {
    local token=$1
    local ids=$2
    local projects_dir=$3
    # Web-аргументы опциональны (обратная совместимость с тестами и
    # Telegram-only установкой). Пишутся только если заданы.
    local jwt=${4:-}
    local bot_username=${5:-}
    local admin_login=${6:-}
    local admin_password=${7:-}
    # Claude headless-креды и выбранный порт — тоже опциональные хвостовые
    # аргументы. Ключ/токен дают zero-touch авторизацию Claude; WEB_PORT
    # персистится, чтобы повторная установка не перекинула адрес на 8765.
    local anthropic_api_key=${8:-}
    local claude_oauth_token=${9:-}
    local web_port=${10:-}
    local web_domain=${11:-}
    # Fernet-ключ Connect Services — опциональный хвостовой аргумент (12).
    local conn_key=${12:-}
    # L-2: Telegram ID оператора-владельца (промоут в админы; фолбэк в
    # приложении — первый ALLOWED_USER_IDS) — опциональный хвостовой аргумент (13).
    local owner_user_id=${13:-}
    cat <<EOF
TELEGRAM_BOT_TOKEN=$token
ALLOWED_USER_IDS=$ids
PROJECTS_DIR=$projects_dir
SESSION_DATABASE_PATH=data/sessions.db
EOF
    [[ -n "$jwt" ]] && printf 'WEB_JWT_SECRET=%s\n' "$jwt"
    [[ -n "$bot_username" ]] && printf 'TELEGRAM_BOT_USERNAME=%s\n' "$bot_username"
    [[ -n "$admin_login" ]] && printf 'ADMIN_LOGIN=%s\n' "$admin_login"
    [[ -n "$admin_password" ]] && printf 'ADMIN_PASSWORD=%s\n' "$admin_password"
    [[ -n "$anthropic_api_key" ]] && printf 'ANTHROPIC_API_KEY=%s\n' "$anthropic_api_key"
    [[ -n "$claude_oauth_token" ]] && printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' "$claude_oauth_token"
    [[ -n "$web_port" ]] && printf 'WEB_PORT=%s\n' "$web_port"
    [[ -n "$web_domain" ]] && printf 'WEB_DOMAIN=%s\n' "$web_domain"
    [[ -n "$conn_key" ]] && printf 'CONNECTIONS_SECRET_KEY=%s\n' "$conn_key"
    [[ -n "$owner_user_id" ]] && printf 'OWNER_USER_ID=%s\n' "$owner_user_id"
    return 0
}

render_web_config() {
    # Локальный override (config/config.local.yaml, вне git) — включает
    # веб-UI. web.enabled/host/public_origin читаются ТОЛЬКО из yaml.
    # host/port по умолчанию — внутренние (127.0.0.1:8765); наружу проксирует
    # Caddy (:80 /agent или домен+HTTPS). cookie_secure не задаём: сервер сам
    # выведет из схемы public_origin (https → Secure-куки; http → нет).
    local public_origin=${1:-}
    local host=${2:-127.0.0.1}
    local port=${3:-8765}
    cat <<EOF
# Сгенерировано install.sh — локальные настройки (вне git, не трогается update).
web:
  enabled: true
  host: "${host}"
  port: ${port}
  public_origin: "${public_origin}"

# Webhook-приёмник (RCE-вектор) держим выключенным явно — defense-in-depth.
webhooks:
  enabled: false
  host: "127.0.0.1"
EOF
}

gen_jwt_secret() {
    # 64 hex-символа. openssl есть почти всегда; иначе python3 (ставится apt).
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        python3 -c "import secrets;print(secrets.token_hex(32))"
    fi
}

gen_connections_key() {
    # Fernet-format key: url-safe base64 of 32 random bytes.
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr '+/' '-_'
    else
        python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    fi
}

detect_public_ip() {
    # Публичный IP сервера для доступа из браузера без домена. Пробуем
    # внешние сервисы, затем локальный адрес. Возвращаем пусто, если не нашли.
    local ip=""
    # Без прокси-переменных (env может содержать чужой прокси — вернул бы его IP).
    local noproxy="env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy"
    if command -v curl >/dev/null 2>&1; then
        ip="$($noproxy curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
        [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || ip="$($noproxy curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || true)"
    fi
    # Фоллбэк: первый ПУБЛИЧНЫЙ адрес из hostname -I (приватные/loopback/docker — мимо).
    # Фоллбэк отсекает приватные диапазоны И CGNAT 100.64.0.0/10 (адрес провайдера
    # за NAT, негодный для браузера снаружи).
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || ip="$(hostname -I 2>/dev/null | tr ' ' '\n' \
        | grep -vE '^(10\.|127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.)' | head -n1 || true)"
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && printf '%s' "$ip" || printf ''
}

render_systemd_unit() {
    local projects_dir="${CFG_PROJECTS_DIR:-${SERVICE_HOME}/projects}"
    # Без токена бота `python -m src.main` не стартует (он валидирует токен и
    # выходит с кодом 1) — сервис уходил бы в рестарт-петлю. Поэтому web-only
    # установка запускается через scripts/run_web.py: тот же стек без Telegram.
    local exec_start="${INSTALL_DIR}/.venv/bin/python -m src.main"
    local unit_description="AI-Panel (Telegram + Web)"
    if [[ -z "${CFG_TOKEN:-}" ]]; then
        exec_start="${INSTALL_DIR}/.venv/bin/python ${INSTALL_DIR}/scripts/run_web.py"
        unit_description="AI-Panel (Web)"
    fi
    cat <<EOF
[Unit]
Description=${unit_description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP:-$SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${exec_start}
Restart=always
RestartSec=5
StartLimitIntervalSec=60
StartLimitBurst=10
ExecStartPre=/bin/sleep 3
Environment=PYTHONUNBUFFERED=1
Environment=HOME=${SERVICE_HOME}
# Sandbox-хардеринг: ограничивает радиус поражения, если Claude (bypassPermissions)
# скомпрометируют через web/инъекцию. Запись разрешена ТОЛЬКО в ReadWritePaths.
#
# H-7: код и .venv принадлежат root (0755) и НЕДОСТУПНЫ на запись сервис-юзеру —
# иначе скомпрометированный Claude мог бы переписать собственный код/venv/.env и
# закрепиться (персистентный бэкдор, переживающий рестарт). Поэтому код помечен
# ReadOnlyPaths, а запись разрешена точечно: data/ (БД сессий + scratch, куда
# Claude пишет в no-project режиме), HOME (~/.claude — состояние CLI) и каталог
# проектов. EnvironmentFile systemd читает САМ (от root, до сброса привилегий),
# приложению .env нужен только на ЧТЕНИЕ (640 root:<group>) — write на него нет.
ReadWritePaths=${INSTALL_DIR}/data ${SERVICE_HOME} ${projects_dir}
ReadOnlyPaths=${INSTALL_DIR}
NoNewPrivileges=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
# ВАЖНО (проверено вживую): НЕ добавляем ProtectHome=true / ProtectSystem=strict /
# RestrictAddressFamilies. ProtectHome=true маскирует /home и ломает доступ к
# каталогу проектов (по умолчанию ${SERVICE_HOME}/projects под /home) — сервис
# падает с PermissionError на старте. Защиту кода от перезаписи даёт связка
# ReadOnlyPaths=${INSTALL_DIR} + root-владение (lock_down_install_dir) — её
# достаточно для H-7; перечисленные директивы ломали запуск без выигрыша.
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=multi-user.target
EOF
}

claude_auth_command() {
    printf 'sudo -u %s -H env HOME=%s claude' "$SERVICE_USER" "$SERVICE_HOME"
}

claude_probe_command() {
    printf 'sudo -u %s -H env HOME=%s claude -p ping --output-format stream-json --verbose' "$SERVICE_USER" "$SERVICE_HOME"
}

run_as_service_user() {
    # M-4: опциональный таймаут (VELS_RUN_TIMEOUT=<секунды>) — для точечных
    # вызовов, которые НЕ должны виснуть навсегда (напр. claude -p ping ДО
    # онбординга на медленной/заблокированной сети). Способ запуска
    # (runuser/sudo) не меняется — timeout лишь оборачивает его.
    local -a prefix=()
    [[ -n "${VELS_RUN_TIMEOUT:-}" ]] && prefix=(timeout "$VELS_RUN_TIMEOUT")
    if [[ $EUID -eq 0 ]]; then
        "${prefix[@]}" runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" USER="$SERVICE_USER" LOGNAME="$SERVICE_USER" "$@"
    else
        "${prefix[@]}" sudo -u "$SERVICE_USER" -H env HOME="$SERVICE_HOME" USER="$SERVICE_USER" LOGNAME="$SERVICE_USER" "$@"
    fi
}

check_os() {
    [[ "$(uname -s)" == "Linux" ]] || die "This installer supports Linux only."
    command -v apt-get >/dev/null 2>&1 || die "apt-get is required. Use Ubuntu or Debian."
    command -v systemctl >/dev/null 2>&1 || die "systemctl is required."
    [[ -d /run/systemd/system ]] || die "systemd is not active on this host."
}

ensure_root() {
    [[ $EUID -eq 0 ]] || die "Run through sudo: curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo bash"
    SUDO=""
}

apt_install_missing() {
    local missing=()
    local pkg
    for pkg in "$@"; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
    done
    if ((${#missing[@]} > 0)); then
        log_info "Installing packages: ${missing[*]}"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    fi
}

# ВАЖНО: без пайпа. "apt-cache policy nodejs | grep -q nodesource" под
# set -o pipefail падает именно тогда, когда репозиторий НАЙДЕН: grep -q выходит
# на первом совпадении и закрывает пайп, apt-cache ловит SIGPIPE и отдаёт 141,
# pipefail берёт худший код в пайпе — и проверка валит здоровую установку,
# пропуская ровно ту поломку, ради которой писалась. Поймано прогоном на живом
# сервере. Сравнение подстроки в bash пайпа не создаёт вовсе.
nodesource_repo_present() {
    local policy
    policy="$(apt-cache policy nodejs 2>/dev/null || true)"
    [[ "$policy" == *nodesource* ]]
}

ensure_node_runtime() {
    local major=""
    if command -v node >/dev/null 2>&1; then
        major="$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || true)"
    fi
    if [[ "$major" =~ ^[0-9]+$ && "$major" -ge 18 ]] && command -v npm >/dev/null 2>&1; then
        log_ok "Node.js $(node --version) and npm $(npm --version)"
        return 0
    fi

    log_info "Installing Node.js 20.x for Claude Code CLI..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    # Скрипт NodeSource может завершиться с кодом 0, не добавив репозиторий
    # (прерван, сменился формат, отвалилась сеть на его же apt-get update).
    # Тогда следующая строка молча поставит nodejs из штатного репозитория
    # Ubuntu 22.04 — это версия 12, вообще без npm. Проверка ниже поймала бы
    # это, но в системе уже остался мусорный пакет и сломанное состояние apt.
    # Проверяем ДО установки: репозиторий есть — ставим, нет — выходим чисто.
    nodesource_repo_present         || die "Репозиторий NodeSource не добавился (deb.nodesource.com недоступен?). Без него apt поставил бы Node.js 12 из штатного репозитория Ubuntu — Claude Code CLI на нём не работает. Повторите установку."
    apt-get install -y -qq nodejs

    major="$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || true)"
    [[ "$major" =~ ^[0-9]+$ && "$major" -ge 18 ]] || die "Node.js 18+ is required for Claude Code CLI."
    command -v npm >/dev/null 2>&1 || die "npm is required for Claude Code CLI."
    log_ok "Node.js $(node --version) and npm $(npm --version)"
}

create_service_user_if_needed() {
    (( SERVICE_NEEDS_CREATE == 1 )) || return 0
    if id -u "$SERVICE_USER" >/dev/null 2>&1; then
        log_ok "Service user exists: $SERVICE_USER"
        return 0
    fi
    useradd --system --create-home \
        --user-group \
        --home-dir "$SERVICE_HOME" \
        --shell /usr/sbin/nologin \
        --comment "AI-Panel service account" \
        "$SERVICE_USER"
    SERVICE_GROUP="$SERVICE_USER"
    log_ok "Created service user: $SERVICE_USER"
}

seed_claude_project_mcp_trust() {
    # M-6: свежий сервис-юзер получает пустой HOME без ~/.claude/settings.json —
    # headless-Claude (без TTY) без этого требует ручного одобрения
    # project-scope MCP-серверов из .mcp.json. Сидим флаг идемпотентно
    # (read-modify-write через python3 json) — существующие ключи НЕ трогаем.
    # Джейл-изоляция флага (чтобы он не протёк в confined-сессии) — отдельная
    # зона (scripts/vels-claude-jail.sh), НЕ здесь.
    command -v python3 >/dev/null 2>&1 || { log_warn "python3 не найден — пропускаю MCP-сид ~/.claude/settings.json."; return 0; }
    [[ -n "$SERVICE_HOME" ]] || return 0
    local settings_dir="$SERVICE_HOME/.claude"
    local settings_file="$settings_dir/settings.json"
    mkdir -p "$settings_dir"
    python3 - "$settings_file" <<'PYEOF'
import json
import os
import sys
import tempfile

path = sys.argv[1]
data = {}
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if content:
            data = json.loads(content)
    except (json.JSONDecodeError, OSError):
        data = {}
if not isinstance(data, dict):
    data = {}
if "enableAllProjectMcpServers" not in data:
    data["enableAllProjectMcpServers"] = True
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise
PYEOF
    chown "$SERVICE_USER":"${SERVICE_GROUP:-$SERVICE_USER}" "$settings_dir" "$settings_file" 2>/dev/null || true
    chmod 0600 "$settings_file" 2>/dev/null || true
    log_ok "MCP project-scope доверие сидировано: $settings_file"
}

ensure_claude_cli() {
    if command -v claude >/dev/null 2>&1; then
        CLAUDE_BIN_PATH="$(command -v claude)"
        log_ok "Claude Code CLI: $CLAUDE_BIN_PATH"
        return 0
    fi
    command -v npm >/dev/null 2>&1 || die "npm is required to install Claude Code CLI."
    log_info "Installing Claude Code CLI with npm..."
    npm install -g @anthropic-ai/claude-code
    command -v claude >/dev/null 2>&1 || die "Claude Code CLI was installed but is not in PATH."
    CLAUDE_BIN_PATH="$(command -v claude)"
    log_ok "Claude Code CLI installed: $CLAUDE_BIN_PATH"
}

resolve_claude_credentials() {
    # Headless-креды Claude для zero-touch авторизации (без браузера).
    # Приоритет: окружение → сохранённое в .env (повторная установка).
    CFG_ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
    [[ -n "$CFG_ANTHROPIC_API_KEY" ]] || CFG_ANTHROPIC_API_KEY="$(existing_env_value ANTHROPIC_API_KEY)"
    CFG_CLAUDE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
    [[ -n "$CFG_CLAUDE_OAUTH_TOKEN" ]] || CFG_CLAUDE_OAUTH_TOKEN="$(existing_env_value CLAUDE_CODE_OAUTH_TOKEN)"
}

claude_auth_status_human() {
    case "${CLAUDE_AUTH_STATUS:-}" in
        key) printf 'через ANTHROPIC_API_KEY — ручной вход не нужен' ;;
        token) printf 'через CLAUDE_CODE_OAUTH_TOKEN — ручной вход не нужен' ;;
        session) printf 'уже авторизован для %s' "$SERVICE_USER" ;;
        missing) printf 'НЕ авторизован — требуется действие (см. ниже)' ;;
        *) printf 'неизвестно' ;;
    esac
}

verify_claude_auth() {
    step "Claude Code authorization"
    # НЕ блокирует установку: бот и веб ставятся в любом случае, статус авторизации
    # отражается в финале. Интерактивный вход в Claude (браузер/код) невозможно
    # пройти внутри `curl|bash` для нового системного пользователя — поэтому
    # настоящий zero-touch достигается через ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN
    # (пишутся в .env → systemd → дочерний claude их наследует).
    if [[ -n "$CFG_ANTHROPIC_API_KEY" ]]; then
        CLAUDE_AUTH_STATUS="key"
        log_ok "Claude авторизуется через ANTHROPIC_API_KEY (.env) — ручной вход не нужен"
        return 0
    fi
    if [[ -n "$CFG_CLAUDE_OAUTH_TOKEN" ]]; then
        CLAUDE_AUTH_STATUS="token"
        log_ok "Claude авторизуется через CLAUDE_CODE_OAUTH_TOKEN (.env) — ручной вход не нужен"
        return 0
    fi
    # M-4: без таймаута `claude -p ping` может повиснуть навсегда на
    # медленной/заблокированной сети ДО онбординга — установщик молча замирает.
    # timeout 30 через run_as_service_user (тот же runuser/sudo, см. функцию).
    local probe_rc=0
    set +e
    VELS_RUN_TIMEOUT=30 run_as_service_user claude -p "ping" --output-format stream-json --verbose >/dev/null 2>&1
    probe_rc=$?
    set -e
    if [[ $probe_rc -eq 0 ]]; then
        CLAUDE_AUTH_STATUS="session"
        log_ok "Claude Code уже авторизован для $SERVICE_USER"
        return 0
    fi
    CLAUDE_AUTH_STATUS="missing"
    if [[ $probe_rc -eq 124 ]]; then
        log_warn "Проверка авторизации Claude не ответила за 30с (таймаут, сеть?) — считаю НЕ авторизованным."
    else
        log_warn "Claude Code НЕ авторизован для $SERVICE_USER."
    fi
    log_warn "Бот и веб установятся, но Claude не будет отвечать, пока не авторизуете (см. финал установки)."
    return 0
}

getme_check() {
    local token=$1
    if [[ -n "${VELS_GETME_MOCK:-}" ]]; then
        printf '%s' "$VELS_GETME_MOCK"
        return 0
    fi
    local response
    response="$(curl -fsS --max-time 10 "https://api.telegram.org/bot${token}/getMe")" || {
        log_err "Could not reach Telegram API. Check network and firewall."
        return 1
    }
    if ! grep -q '"ok"[[:space:]]*:[[:space:]]*true' <<<"$response"; then
        log_err "Telegram rejected the token: $response"
        return 1
    fi
    local username
    username="$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$response")"
    printf '%s' "${username:-unknown}"
}

default_projects_dir() {
    printf '%s/projects' "${SERVICE_HOME:-$HOME}"
}

prompt_onboarding() {
    step "AI-Panel configuration"
    # Спрашиваем ТОЛЬКО то, что уникально для клиента: токен бота и его
    # Telegram ID. Остальное (папка проектов, админ-пароль, адрес) — авто.
    # Любое значение можно передать через окружение → установка без вопросов:
    #   curl ... | sudo TELEGRAM_BOT_TOKEN=.. ALLOWED_USER_IDS=.. bash
    local token ids parsed expanded
    local target_home="${SERVICE_HOME:-$HOME}"
    # M-3: повторный install.sh на уже установленном хосте — де-факто "update"
    # для release-установок (update.sh отправляет их именно сюда, см. его
    # заключительное сообщение). Без этого фолбэка каждое такое обновление
    # заново спрашивало токен/ID (или падало с "Нет терминала" в curl|bash).
    local existing_token existing_ids
    existing_token="$(existing_env_value TELEGRAM_BOT_TOKEN)"
    existing_ids="$(existing_env_value ALLOWED_USER_IDS)"

    # --- 1/2 Telegram bot token ---
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        # Токен из окружения (Вариант 1, без вопросов): на ошибку выходим с
        # понятным сообщением — переспрашивать некого, человек поправит команду.
        validate_token_format "$TELEGRAM_BOT_TOKEN" >/dev/null 2>&1 \
            || die "TELEGRAM_BOT_TOKEN из окружения имеет неверный формат."
        CFG_TOKEN="$TELEGRAM_BOT_TOKEN"
        log_info "Проверяю токен через Telegram getMe..."
        CFG_BOT_USERNAME="$(getme_check "$CFG_TOKEN")" \
            || die "Telegram отверг токен из окружения — проверьте TELEGRAM_BOT_TOKEN."
    elif [[ -n "$existing_token" ]]; then
        # Уже сохранённый токен (повторная установка) — используем БЕЗ вопроса.
        # getMe здесь МЯГКИЙ: сетевой сбой не должен ронять живую переустановку
        # (токен уже был рабочим ранее) — просто предупреждаем и идём дальше.
        CFG_TOKEN="$existing_token"
        log_info "Использую сохранённый TELEGRAM_BOT_TOKEN из существующего .env"
        log_info "Проверяю токен через Telegram getMe..."
        if CFG_BOT_USERNAME="$(getme_check "$CFG_TOKEN")"; then
            :
        else
            log_warn "Telegram getMe не ответил (сеть?) — использую сохранённый токен как есть."
            CFG_BOT_USERNAME="$(existing_env_value TELEGRAM_BOT_USERNAME)"
            [[ -n "$CFG_BOT_USERNAME" ]] || CFG_BOT_USERNAME="unknown"
        fi
    elif [[ -t 0 || -r /dev/tty ]]; then
        # Живой ввод. Telegram НЕОБЯЗАТЕЛЕН: пустой Enter — установка только с
        # вебом. На ошибку формата/токена просим ввести заново, НЕ выходим из
        # установщика; getme_check ограничен --max-time 10 и не «висит».
        while :; do
            printf '\nTelegram-бот — НЕОБЯЗАТЕЛЬНО (токен от @BotFather, команда /newbot)\n'
            printf '    Вставьте токен и нажмите Enter — или просто Enter, чтобы работать только через веб.\n'
            # L-10: без таймаута голый read висел бы навсегда в detached
            # tmux/screen без живого человека за терминалом. Таймаут = пропуск.
            # 2>/dev/null раньше </dev/tty: без живого терминала сообщение об
            # ошибке открытия tty не должно пугать в логе — это штатный пропуск.
            # Prompt печатаем сами: `read -p` пишет его в stderr, который здесь
            # уходит в /dev/null — человек не видел строку и не понимал, чего
            # от него ждут.
            printf '    Token (Enter — пропустить): '
            read -r -t 900 token 2>/dev/null </dev/tty || token=""
            token="$(sanitize_input "$token")"
            [[ -z "$token" ]] && break
            if ! validate_token_format "$token" >/dev/null 2>&1; then
                log_err "Неверный формат токена (пример: 1234567890:AAFabc…, 35+ символов). Введите заново или Enter, чтобы пропустить."
                continue
            fi
            log_info "Проверяю токен через Telegram getMe..."
            if CFG_BOT_USERNAME="$(getme_check "$token")"; then
                CFG_TOKEN="$token"
                break
            fi
            log_err "Токен не принят Telegram (неверный токен или нет связи). Введите заново или Enter, чтобы пропустить."
        done
    else
        log_info "Нет терминала и не задан TELEGRAM_BOT_TOKEN — ставлю только веб-интерфейс."
    fi
    if [[ -n "$CFG_TOKEN" ]]; then
        log_ok "Telegram bot: @${CFG_BOT_USERNAME}"
    else
        CFG_BOT_USERNAME=""
        log_ok "Telegram не настроен — интерфейс только веб"
    fi

    if [[ -n "$CFG_TOKEN" ]]; then
        # --- Allowed Telegram user ID(s) ---
        if [[ -n "${ALLOWED_USER_IDS:-}" ]]; then
            parsed="$(parse_user_ids "$ALLOWED_USER_IDS" 2>/dev/null)" \
                || die "ALLOWED_USER_IDS из окружения некорректен (только числа через запятую)."
            CFG_IDS="$parsed"
        elif [[ -n "$existing_ids" ]] && parsed="$(parse_user_ids "$existing_ids" 2>/dev/null)"; then
            # M-3: как и с токеном — переиспользуем сохранённое значение без вопроса.
            CFG_IDS="$parsed"
            log_info "Использую сохранённый ALLOWED_USER_IDS из существующего .env"
        else
            # Токен пришёл из окружения — значит установку кто-то скриптует, и
            # отвечать на вопросы некому. Спрашивать ID в этом режиме
            # бессмысленно: установщик просто зациклится (поймано прогоном на
            # живом сервере — вопрос повторялся бесконечно, по 15 минут таймаута
            # на итерацию). Падаем сразу и показываем готовую команду.
            if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
                die "TELEGRAM_BOT_TOKEN задан через окружение, а ALLOWED_USER_IDS — нет. В неинтерактивной установке ID спросить не у кого. Передайте оба: curl -sSL <бутстрап> | sudo TELEGRAM_BOT_TOKEN=<токен> ALLOWED_USER_IDS=<ваш id> bash"
            fi
            [[ -t 0 || -r /dev/tty ]] || die "Нет терминала и не задан ALLOWED_USER_IDS. Передайте его через окружение."
            # Ограничение попыток: пустой Enter здесь (в отличие от вопроса про
            # токен) не пропуск, а ошибка ввода, и без счётчика цикл не имеет
            # выхода вообще.
            local id_attempts=0
            while :; do
                printf '\nВаш Telegram user ID (узнать у @userinfobot; несколько: 111,222)\n'
                printf '    Вставьте ID и нажмите Enter.\n'
                read -r -t 900 -p '    User ID(s): ' ids </dev/tty \
                    || die "Ввод ID прерван или не получен за 15 минут — передайте ALLOWED_USER_IDS через окружение."
                ids="$(sanitize_input "$ids")"
                if parsed="$(parse_user_ids "$ids" 2>/dev/null)"; then CFG_IDS="$parsed"; break; fi
                id_attempts=$(( id_attempts + 1 ))
                if (( id_attempts >= 5 )); then
                    die "ID не введён за 5 попыток. Передайте его через окружение: ALLOWED_USER_IDS=<ваш id>, либо оставьте Telegram неподключённым (веб работает и без него)."
                fi
                log_err "Только числовые Telegram ID через запятую (осталось попыток: $(( 5 - id_attempts )))."
            done
        fi
        log_ok "Allowed IDs: $CFG_IDS"

        # --- L-2: OWNER_USER_ID — какой из ALLOWED_USER_IDS оператор назвал СВОИМ
        # (промоут владельца в админы; фолбэк в приложении — первый allowed id).
        # Явный env — высший приоритет; иначе сохранённое значение (повторная
        # установка); иначе первый id из списка выше (тот же порядок, что "Ваш
        # Telegram user ID" в вопросе 2/2 — первым вводится свой, затем остальные).
        if [[ -n "${OWNER_USER_ID:-}" ]]; then
            CFG_OWNER_USER_ID="$OWNER_USER_ID"
        else
            CFG_OWNER_USER_ID="$(existing_env_value OWNER_USER_ID)"
            [[ -n "$CFG_OWNER_USER_ID" ]] || CFG_OWNER_USER_ID="${CFG_IDS%%,*}"
        fi
        log_ok "Owner user id (промоут в админы): $CFG_OWNER_USER_ID"
    else
        # Без бота Telegram-операторов нет: список остаётся пустым, вход в веб
        # идёт логином и паролем администратора (ADMIN_LOGIN/ADMIN_PASSWORD).
        CFG_IDS=""
        CFG_OWNER_USER_ID=""
    fi

    # --- (опционально) Claude API-ключ для авторизации ---
    # Спрашиваем ТОЛЬКО если Claude ещё НЕ авторизован (нет ключа/токена/сессии)
    # и есть терминал. Раньше ключ передавали только через ENV, и кто ставил без
    # него — упирался в «бот не отвечает». Теперь предлагаем ввести сразу; Enter —
    # пропустить (в конце покажем, как добавить позже).
    if [[ "${CLAUDE_AUTH_STATUS:-}" == "missing" && -z "$CFG_ANTHROPIC_API_KEY" \
          && -z "$CFG_CLAUDE_OAUTH_TOKEN" && ( -t 0 || -r /dev/tty ) ]]; then
        local api_key
        printf '\nClaude API-ключ (Anthropic — console.anthropic.com/keys).\n'
        printf '    Нужен, чтобы бот отвечал (оплата по факту использования).\n'
        printf '    Вставьте ключ и нажмите Enter — или просто Enter, чтобы пропустить.\n'
        # L-10: таймаут тоже трактуется как "пропустить" (Enter) — не die,
        # это опциональный вопрос.
        read -r -t 900 -p '    API key (sk-ant-...): ' api_key </dev/tty || api_key=""
        api_key="$(sanitize_input "$api_key")"
        if [[ -n "$api_key" ]]; then
            CFG_ANTHROPIC_API_KEY="$api_key"
            CLAUDE_AUTH_STATUS="key"
            log_ok "Claude будет авторизован через ANTHROPIC_API_KEY (.env)"
        else
            log_info "API-ключ пропущен — добавите позже (инструкция в конце установки)."
        fi
    fi

    # --- Папка проектов: env PROJECTS_DIR или дефолт, БЕЗ вопроса ---
    if [[ -n "${PROJECTS_DIR:-}" ]] \
        && expanded="$(HOME="$target_home" expand_absolute_path "$PROJECTS_DIR" 2>/dev/null)"; then
        CFG_PROJECTS_DIR="$expanded"
    else
        CFG_PROJECTS_DIR="$(default_projects_dir)"
    fi
    log_ok "Projects dir: $CFG_PROJECTS_DIR"
}

existing_env_value() {
    # Достаёт значение ключа из существующего .env (или пусто).
    local key=$1
    local file=${2:-$INSTALL_DIR/.env}
    [[ -f "$file" ]] || return 0
    # awk вместо `sed | head` — без SIGPIPE (head закрывает пайп → set -e/pipefail
    # мог прервать установку). exit после первого совпадения.
    awk -F= -v k="$key" '$1==k{sub(/^[^=]*=/,"");print;exit}' "$file"
}

gen_admin_password() {
    # L-13: было `openssl rand -hex 6` = 48 бит энтропии (12 hex-символов) —
    # перебираемо. Теперь 18 случайных байт = 144 бита (24 символа base64url).
    # base64url (без +/=) безопасен в .env как ADMIN_PASSWORD=<значение> (нет
    # спецсимволов dotenv) и в URL/копипасте. Показывается один раз в финале.
    if command -v openssl >/dev/null 2>&1; then
        # tr → base64url-алфавит, срезаем возможный '=' padding (18 байт → 24 симв., без '=').
        openssl rand -base64 18 | tr '+/' '-_' | tr -d '='
    else
        python3 -c "import secrets;print(secrets.token_urlsafe(18))"
    fi
}

port_in_use() {
    local p=$1
    local listening
    # Тот же SIGPIPE-капкан, что и в nodesource_repo_present: grep -q закрывает
    # пайп на первом совпадении, ss/awk получают SIGPIPE, pipefail превращает
    # это в 141 — и ЗАНЯТЫЙ порт выглядит свободным. Собираем вывод в
    # переменную и сравниваем без пайпа.
    if command -v ss >/dev/null 2>&1; then
        listening="$(ss -ltnH 2>/dev/null | awk '{print $4}' || true)"
    elif command -v netstat >/dev/null 2>&1; then
        listening="$(netstat -ltn 2>/dev/null | awk '{print $4}' || true)"
    else
        return 1
    fi
    grep -qE "[:.]${p}\$" <<<"$listening"
}

pick_web_port() {
    # Идемпотентность: при повторной установке предпочитаем РАНЕЕ выбранный порт
    # из .env. Иначе на re-run свой же активный сервис держит :80 → port_in_use=true
    # → молчаливый откат на 8765 и слом сохранённого пользователем адреса.
    # Явный env WEB_PORT всё равно имеет приоритет (осознанная смена порта).
    if [[ -z "${WEB_PORT:-}" ]]; then
        local prev
        prev="$(existing_env_value WEB_PORT)"
        if [[ "$prev" =~ ^[0-9]+$ ]]; then printf '%s' "$prev"; return 0; fi
    fi
    # Внутренний порт приложения (наружу — Caddy). По умолчанию 8765.
    local want="${WEB_PORT:-8765}"
    if [[ "$want" == "8765" ]] && port_in_use 8765; then
        log_warn "Порт 8765 занят — использую 8766 (внутренний)."
        printf '8766'
    else
        printf '%s' "$want"
    fi
}

build_ip_origin() {
    # Доступ по IP идёт через Caddy на :80 под путём /agent.
    local ip=$1
    printf 'http://%s/agent' "$ip"
}

vite_base_for_origin() {
    # База фронта выводится из origin: путь /agent → /agent/, иначе корень.
    local o=$1
    [[ "$o" == */agent || "$o" == */agent/ ]] && printf '/agent/' || printf '/'
}

sslip_host_for_ip() {
    # Бесплатное DNS-имя для IP без покупки домена: <ip>.sslip.io резолвится
    # обратно в этот IP, поэтому Caddy может выпустить НАСТОЯЩИЙ TLS-сертификат
    # Let's Encrypt (HTTPS с зелёным замком, без своего домена).
    local ip=${1:-}
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    printf '%s.sslip.io' "$ip"
}

resolve_web_mode() {
    # Режим (ip|domain|ip_tls), public_origin, VITE_BASE.
    #  - WEB_MODE=ip — явный небезопасный IP-режим по http (zero-touch opt-out).
    #  - Домен: env DOMAIN, иначе hostname из PUBLIC_ORIGIN=https://..., иначе пусто.
    #  - Непустой PUBLIC_ORIGIN без валидного домена — уважаем как есть (НЕ затираем).
    #  - WEB_TLS=internal — самоподписанный TLS на голый IP (https://<ip>),
    #    шифрование без внешних зависимостей (браузер один раз спросит).
    #  - По умолчанию БЕЗ домена → HTTPS через <ip>.sslip.io (Let's Encrypt).
    local ip=${1:-}
    CFG_PERSIST_DOMAIN=""
    if [[ "${WEB_MODE:-}" == "ip" ]]; then
        CFG_DOMAIN=""; CFG_WEB_MODE="ip"
        CFG_PUBLIC_ORIGIN="${PUBLIC_ORIGIN:-$(build_ip_origin "$ip")}"
        CFG_VITE_BASE="$(vite_base_for_origin "$CFG_PUBLIC_ORIGIN")"
        return 0
    fi
    local domain="${DOMAIN:-}"
    if [[ -z "$domain" && "${PUBLIC_ORIGIN:-}" == https://* ]]; then
        domain="${PUBLIC_ORIGIN#https://}"; domain="${domain%%/*}"
    fi
    if [[ -n "$domain" ]] && validate_domain_format "$domain" >/dev/null 2>&1; then
        CFG_DOMAIN="$domain"; CFG_WEB_MODE="domain"
        CFG_PERSIST_DOMAIN="$domain"  # реальный домен — персистим в .env
        CFG_PUBLIC_ORIGIN="${PUBLIC_ORIGIN:-https://$domain}"
        CFG_VITE_BASE="/"
    elif [[ -n "${PUBLIC_ORIGIN:-}" ]]; then
        CFG_DOMAIN=""; CFG_WEB_MODE="ip"
        CFG_PUBLIC_ORIGIN="$PUBLIC_ORIGIN"
        CFG_VITE_BASE="$(vite_base_for_origin "$PUBLIC_ORIGIN")"
    elif [[ "${WEB_TLS:-}" == "internal" ]] && [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        # Самоподписанный TLS на голый IP — шифрование без sslip.io/Let's Encrypt.
        CFG_DOMAIN="$ip"; CFG_WEB_MODE="ip_tls"
        CFG_PUBLIC_ORIGIN="https://$ip"
        CFG_VITE_BASE="/"
    else
        # Дефолт без домена: HTTPS через <ip>.sslip.io (настоящий сертификат).
        local sslip
        if sslip="$(sslip_host_for_ip "$ip")"; then
            CFG_DOMAIN="$sslip"; CFG_WEB_MODE="domain"
            # НЕ персистим авто-домен: при смене IP re-run пересоберёт из текущего.
            CFG_PUBLIC_ORIGIN="https://$sslip"
            CFG_VITE_BASE="/"
        else
            # IP не определён/не IPv4 — деградируем к http по IP (как раньше).
            CFG_DOMAIN=""; CFG_WEB_MODE="ip"
            CFG_PUBLIC_ORIGIN="$(build_ip_origin "$ip")"
            CFG_VITE_BASE="/agent/"
        fi
    fi
}

validate_domain_format() {
    local d=${1:-}
    [[ -n "$d" ]] || return 1
    [[ "$d" != *"://"* && "$d" != */* && "$d" != *" "* ]] || return 1
    [[ "$d" == *.* ]] || return 1
    [[ "$d" =~ ^[A-Za-z0-9.-]+$ ]] || return 1
    # Голый IPv4 — не домен (иначе Caddy/Let's Encrypt обречён выпускать сертификат на IP).
    [[ "$d" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && return 1
    printf 'ok'
}

render_caddyfile() {
    # Caddy — «входная дверь». Режимы:
    #   domain  — блок домена/<ip>.sslip.io с авто-HTTPS (Let's Encrypt) + HSTS.
    #   ip_tls  — голый IP с самоподписанным TLS (tls internal), без HSTS.
    #   ip      — http://<ip>/agent без TLS (небезопасно, opt-out).
    local mode=$1
    local domain=${2:-}
    local port=${3:-8765}
    # Security-заголовки (H-5/L-2). CSP: фронт Vite/React (внешний хэш-бандл →
    # script-src 'self'; инлайн style-атрибуты React → style-src 'unsafe-inline'),
    # /login грузит telegram-widget (telegram.org) + iframe oauth.telegram.org.
    # HSTS — ТОЛЬКО на TLS-блоке (по http браузер его игнорирует, держать незачем).
    local csp="default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; frame-src https://oauth.telegram.org https://telegram.org; base-uri 'self'; frame-ancestors 'none'"
    local sec_headers_tls='    header {
        -Server
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "no-referrer"
        Content-Security-Policy "'"$csp"'"
    }'
    local sec_headers_notls='    header {
        -Server
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "no-referrer"
        Content-Security-Policy "'"$csp"'"
    }'
    if [[ "$mode" == "domain" ]]; then
        cat <<EOF
# Сгенерировано install.sh. Авто-HTTPS (Let's Encrypt) для домена/<ip>.sslip.io.
${domain} {
    encode zstd gzip
${sec_headers_tls}
    reverse_proxy 127.0.0.1:${port}
}
EOF
    elif [[ "$mode" == "ip_tls" ]]; then
        cat <<EOF
# Сгенерировано install.sh. Самоподписанный TLS (tls internal) на голый IP.
# Браузер один раз предупредит о неизвестном CA — это ожидаемо.
https://${domain} {
    tls internal
    encode zstd gzip
${sec_headers_notls}
    reverse_proxy 127.0.0.1:${port}
}
EOF
    else
        cat <<EOF
# Сгенерировано install.sh. Доступ по http://<ip>/agent БЕЗ шифрования
# (небезопасно — пароль/сессия открытым текстом). На :80 рядом могут жить
# другие приложения (добавляйте свои блоки handle_path / маршруты).
:80 {
    encode zstd gzip
${sec_headers_notls}
    redir /agent /agent/
    handle_path /agent/* {
        reverse_proxy 127.0.0.1:${port}
    }
}
EOF
    fi
}

prompt_web_admin() {
    step "Web UI access"
    # БЕЗ вопросов: всё авто (или из окружения).
    # JWT — сохраняем существующий при повторной установке (не разлогинить всех).
    CFG_JWT_SECRET="$(existing_env_value WEB_JWT_SECRET)"
    [[ -n "$CFG_JWT_SECRET" ]] || CFG_JWT_SECRET="$(gen_jwt_secret)"

    # CONNECTIONS_SECRET_KEY — Fernet-ключ шифрования секретов Connect Services.
    # Сохраняем существующий при повторной установке: ротация осиротит ВСЕ
    # зашифрованные секреты пользователей (расшифровать станет невозможно).
    CFG_CONNECTIONS_KEY="$(existing_env_value CONNECTIONS_SECRET_KEY)"
    [[ -n "$CFG_CONNECTIONS_KEY" ]] || CFG_CONNECTIONS_KEY="$(gen_connections_key)"

    # Логин админа: env ADMIN_LOGIN или 'admin'.
    CFG_ADMIN_LOGIN="${ADMIN_LOGIN:-admin}"

    # Пароль: env ADMIN_PASSWORD → сохранённый в .env → сгенерировать (и показать).
    CFG_ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
    [[ -n "$CFG_ADMIN_PASSWORD" ]] || CFG_ADMIN_PASSWORD="$(existing_env_value ADMIN_PASSWORD)"
    if [[ -z "$CFG_ADMIN_PASSWORD" ]]; then
        CFG_ADMIN_PASSWORD="$(gen_admin_password)"
        CFG_ADMIN_PASSWORD_GENERATED=1
    fi

    # Внутренний адрес приложения (наружу — через Caddy).
    CFG_WEB_HOST="127.0.0.1"
    CFG_WEB_PORT="$(pick_web_port)"

    # Сохранённый домен с прошлой установки (чтобы re-run не сбросил https→IP).
    # WEB_MODE=ip — явный IP без вопросов; env DOMAIN/PUBLIC_ORIGIN — приоритет.
    if [[ "${WEB_MODE:-}" != "ip" && -z "${DOMAIN:-}" && -z "${PUBLIC_ORIGIN:-}" ]]; then
        local prev_domain; prev_domain="$(existing_env_value WEB_DOMAIN)"
        [[ -n "$prev_domain" ]] && DOMAIN="$prev_domain"
    fi
    # Домен: env DOMAIN/PUBLIC_ORIGIN/сохранённый, иначе спрашиваем (Enter → по IP).
    if [[ "${WEB_MODE:-}" != "ip" && -z "${DOMAIN:-}" && -z "${PUBLIC_ORIGIN:-}" && ( -t 0 || -r /dev/tty ) ]]; then
        local dom
        while :; do
            printf '\nЕсть свой домен? Впишите его. Если домена НЕТ — оставьте поле пустым (просто Enter) → доступ по IP.\n'
            # L-10: таймаут (как и пустой Enter) трактуется как "домена нет" —
            # безопасный дефолт (доступ по IP/sslip.io), без die.
            read -r -t 900 -p '    Домен: ' dom </dev/tty || dom=""
            dom="$(sanitize_input "$dom")"
            [[ -z "$dom" ]] && break
            validate_domain_format "$dom" >/dev/null 2>&1 && { DOMAIN="$dom"; break; }
            log_err "Неверный формат домена (пример: claude.example.com), либо Enter чтобы пропустить."
        done
    fi

    local ip
    ip="$(detect_public_ip)"
    if [[ -z "$ip" && -z "${DOMAIN:-}" && -z "${PUBLIC_ORIGIN:-}" ]]; then
        die "Не удалось определить публичный IP. Задайте DOMAIN или PUBLIC_ORIGIN в окружении."
    fi
    resolve_web_mode "$ip"

    if [[ "$CFG_WEB_MODE" == "domain" && "$CFG_DOMAIN" == *.sslip.io ]]; then
        log_warn "Без домена: HTTPS через $CFG_DOMAIN — Caddy выпустит настоящий TLS-сертификат (Let's Encrypt)."
        log_warn "Условие: порты 80 и 443 доступны снаружи (открываем их в firewall/security group)."
    elif [[ "$CFG_WEB_MODE" == "domain" ]]; then
        log_warn "Домен $CFG_DOMAIN: Caddy выпустит TLS-сертификат автоматически."
        log_warn "Условие: домен УЖЕ указывает на IP сервера и открыты порты 80+443."
    elif [[ "$CFG_WEB_MODE" == "ip_tls" ]]; then
        log_warn "Самоподписанный TLS на $CFG_PUBLIC_ORIGIN — трафик шифрован, но браузер один раз предупредит о сертификате."
    else
        log_warn "ВНИМАНИЕ: доступ по IP по http БЕЗ шифрования — пароль/сессия открытым текстом (вы выбрали WEB_MODE=ip)."
    fi
    log_ok "Web: ${CFG_PUBLIC_ORIGIN} · admin: ${CFG_ADMIN_LOGIN}"
}

build_frontend() {
    step "Frontend build (web UI)"
    CFG_WEB_BUILT=0
    if [[ ! -f "$INSTALL_DIR/web/package.json" ]]; then
        log_warn "web/ отсутствует — пропускаю сборку фронта (репо без веб-UI)."
        return 0
    fi
    # НЕ фатально: если фронт не собрался — Telegram-бот всё равно поставится,
    # просто веб-UI будет недоступен (mount_frontend = no-op без web/dist).
    # CFG_WEB_BUILT нужен, чтобы финал не печатал зелёный URL при несобранном вебе.
    # H-7: сборка идёт от ROOT (web/ принадлежит root, сервис-юзер пишет туда не
    # может). npm ci (не install) ставит ровно из package-lock.json — детерминизм.
    # Имя бота в сборку НЕ прокидываем: страница входа спрашивает его у сервера
    # (/api/auth/config), поэтому одна и та же сборка обслуживает и установку с
    # Telegram, и веб-only. Подключение бота к готовой инсталляции меняет .env —
    # пересборка фронта для этого не нужна. Виджет входа требует ЕЩЁ и ручного
    # BotFather /setdomain — это вне зоны установщика.
    if bash -lc "cd '$INSTALL_DIR/web' && npm ci && VITE_BASE='$CFG_VITE_BASE' npm run build" \
        && [[ -f "$INSTALL_DIR/web/dist/index.html" ]]; then
        CFG_WEB_BUILT=1
        log_ok "Frontend собран: $INSTALL_DIR/web/dist"
    else
        log_warn "Сборка фронта не удалась — бот установится, веб-UI будет недоступен."
        log_warn "Починить: cd $INSTALL_DIR/web && npm ci && npm run build, затем systemctl restart $SERVICE_NAME"
    fi
}

write_web_config() {
    step "Web config"
    local cfg_file="$INSTALL_DIR/config/config.local.yaml"
    mkdir -p "$INSTALL_DIR/config"
    render_web_config "$CFG_PUBLIC_ORIGIN" "$CFG_WEB_HOST" "$CFG_WEB_PORT" >"$cfg_file"
    # H-7: config принадлежит root, сервис-юзер только читает (0640). Конфиг
    # статичен (читается на старте), записи сервис-юзеру не требуется.
    chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$cfg_file"
    chmod 640 "$cfg_file"
    log_ok "Web включён в $cfg_file (host=$CFG_WEB_HOST, port=$CFG_WEB_PORT)"
}

detect_ssh_port() {
    # M-5: SSH-порт(ы), чтобы НЕ отрезать себя при включении ufw. Печатает
    # КАЖДЫЙ найденный порт отдельной строкой (может быть несколько) — при
    # неоднозначности открываем ВСЕ кандидаты, а не сужаем до одного/22.
    # Раньше полагались ТОЛЬКО на SSH_CONNECTION (под sudo обычно ПУСТО —
    # env_reset стирает переменные окружения) и /etc/ssh/sshd_config (без
    # sshd_config.d/*.conf — туда cloud-init/провайдеры нередко кладут Port).
    local -A seen=()
    local port f

    # 1) Живой слушатель sshd — самый надёжный источник (реальный bind, а не
    # декларация в конфиге). Требует root (уже гарантировано — ensure_root).
    if command -v ss >/dev/null 2>&1; then
        while IFS= read -r port; do
            [[ "$port" =~ ^[0-9]+$ ]] || continue
            [[ -n "${seen[$port]:-}" ]] && continue
            seen[$port]=1
            printf '%s\n' "$port"
        done < <(ss -tlnpH 2>/dev/null | awk '/sshd/{ n=split($4,a,":"); if (n>0 && a[n] ~ /^[0-9]+$/) print a[n] }')
    fi

    # 2) sshd_config + sshd_config.d/*.conf (drop-in — распространено у
    # cloud-провайдеров/cloud-init, туда `grep /etc/ssh/sshd_config` не заглядывал).
    for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf; do
        [[ -r "$f" ]] || continue
        while IFS= read -r port; do
            [[ "$port" =~ ^[0-9]+$ ]] || continue
            [[ -n "${seen[$port]:-}" ]] && continue
            seen[$port]=1
            printf '%s\n' "$port"
        done < <(grep -iE '^[[:space:]]*Port[[:space:]]+[0-9]+' "$f" 2>/dev/null | awk '{print $2}')
    done

    # 3) SSH_CONNECTION (поле сервер-порта — 4-е: client_ip client_port
    # server_ip server_port) — обычно ПУСТО под `sudo bash` (env_reset), но
    # пригодится при прямом запуске от root по SSH без sudo.
    if [[ -n "${SSH_CONNECTION:-}" ]]; then
        port="$(awk '{print $4}' <<<"$SSH_CONNECTION" 2>/dev/null)"
        if [[ "$port" =~ ^[0-9]+$ && -z "${seen[$port]:-}" ]]; then
            seen[$port]=1
            printf '%s\n' "$port"
        fi
    fi

    # Ничего не нашли — дефолт 22 (старое поведение).
    (( ${#seen[@]} > 0 )) || printf '22\n'
}

open_firewall_port() {
    step "Firewall"
    # Босс просил «максимально застраховано»: включаем ufw с политикой deny по
    # умолчанию, оставляя снаружи только SSH + 80/443. Внутренние сервисы
    # (webhook 8080, web 8765) слушают loopback и наружу НЕ открываются.
    # Отключить целиком: VELS_SKIP_FIREWALL=1.
    if [[ "${VELS_SKIP_FIREWALL:-}" == "1" ]]; then
        log_warn "VELS_SKIP_FIREWALL=1 — firewall не настраивается (порты не закрываются)."
        return 0
    fi
    apt_install_missing ufw >/dev/null 2>&1 || true
    if ! command -v ufw >/dev/null 2>&1; then
        log_warn "ufw недоступен — закройте 8080/8765 и оставьте только SSH+80/443 вручную."
        return 0
    fi
    local ssh_ports; ssh_ports="$(detect_ssh_port)"
    # M-5: SSH разрешаем ПЕРВЫМ и с запасом (ВСЕ найденные порты + 22 + профиль
    # OpenSSH) — при неоднозначности НЕ сужаем до одного порта/22, чтобы
    # включение ufw не отрезало доступ к серверу на нестандартном SSH-порту.
    local p
    while IFS= read -r p; do
        [[ -n "$p" ]] && { ufw allow "${p}/tcp" >/dev/null 2>&1 || true; }
    done <<<"$ssh_ports"
    ufw allow 22/tcp >/dev/null 2>&1 || true
    ufw allow OpenSSH >/dev/null 2>&1 || true
    # Веб: 80 (ACME/redirect) и 443 (HTTPS). 8080(webhook)/8765(web) — только
    # loopback, наружу НЕ открываем.
    ufw allow 80/tcp >/dev/null 2>&1 || true
    ufw allow 443/tcp >/dev/null 2>&1 || true
    ufw default deny incoming >/dev/null 2>&1 || true
    ufw default allow outgoing >/dev/null 2>&1 || true
    local ssh_ports_csv; ssh_ports_csv="$(tr '\n' ',' <<<"$ssh_ports" | sed 's/,$//; s/,,*/,/g')"
    if ufw --force enable >/dev/null 2>&1; then
        log_ok "Firewall включён: SSH(${ssh_ports_csv},22)+80+443 открыты, остальное закрыто (8080/8765 — loopback)"
    else
        log_warn "Не удалось включить ufw — настройте вручную (разрешите только SSH+80+443)."
    fi
    log_info "Если есть облачная security group — откройте в ней 80 и 443."
    # M-5: аварийный выход, если ufw всё же отрезал доступ (нестандартный SSH-порт
    # не был найден/распознан) — переустановка с этой переменной пропускает firewall.
    log_info "Аварийный выход при лок-ауте по SSH: переустановите с VELS_SKIP_FIREWALL=1 (или руками: ufw allow <port>/tcp)."
}

install_or_update_repo() {
    step "Repository"
    # Release-режим (раздача комьюнити): код приходит распакованным архивом —
    # без git и БЕЗ токена. RELEASE_SRC указывает на дерево исходников (его
    # готовит публичный бутстрап: скачал tar.gz → проверил sha256 → распаковал).
    # Копируем поверх INSTALL_DIR, не трогая .env/data/.venv — их в архиве нет,
    # поэтому cp не может их затереть (повторный запуск = безопасное обновление).
    if [[ -n "${RELEASE_SRC:-}" ]]; then
        [[ -f "$RELEASE_SRC/src/main.py" ]] \
            || die "RELEASE_SRC=$RELEASE_SRC не похоже на исходники AI-Panel (нет src/main.py)."
        # Не затираем ЧУЖОЙ непустой каталог (как и git-путь делает die). Нашу
        # установку узнаём по src/main.py; пустой/новый каталог — ок.
        if [[ -e "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" \
              && ! -f "$INSTALL_DIR/src/main.py" ]]; then
            die "$INSTALL_DIR непустой и не похож на установку AI-Panel. Удалите его или задайте INSTALL_DIR=/srv/vels-claude."
        fi
        mkdir -p "$INSTALL_DIR"
        # Сносим stale .git от старой git-установки: её remote с протухшим
        # токеном иначе уводит update.sh в git-ветку вместо release-режима.
        rm -rf "$INSTALL_DIR/.git"
        # Идемпотентная замена ТОЛЬКО релиз-управляемых путей: для каждого
        # верхнеуровневого элемента архива сносим старую версию и копируем свежую.
        # Так исчезают «призраки» (файлы, удалённые ВНУТРИ этих деревьев между
        # релизами), но посторонние файлы рядом (backups/, заметки) и
        # пользовательское состояние (.env/data/.venv) НЕ трогаются — цикл их не
        # касается (их нет в архиве + явный skip ниже на случай будущего архива).
        local _entry _name
        for _entry in "$RELEASE_SRC"/* "$RELEASE_SRC"/.[!.]*; do
            [[ -e "$_entry" ]] || continue
            _name="$(basename "$_entry")"
            case "$_name" in
                .env|data|.venv)
                    # Пользовательское состояние — не трогаем НИКОГДА, даже если
                    # будущий архив по ошибке его включит (защита .env/БД).
                    continue ;;
                config)
                    # config держит tracked-файлы И локальный config.local.yaml:
                    # чистим только tracked, сохраняя локальный конфиг.
                    if [[ -d "$INSTALL_DIR/config" ]]; then
                        find "$INSTALL_DIR/config" -mindepth 1 ! -name 'config.local.yaml' \
                            -exec rm -rf {} + 2>/dev/null || true
                    fi
                    continue ;;
            esac
            rm -rf "$INSTALL_DIR/$_name"
        done
        # `/.` копирует содержимое включая dot-файлы (.env.example), но не сам
        # каталог-источник. .env/data/.venv в архиве отсутствуют → не трогаются.
        cp -a "$RELEASE_SRC"/. "$INSTALL_DIR"/
        # H-7: код принадлежит root. В боевом потоке RELEASE_SRC распакован root'ом
        # (cp -a → root-владение), а lock_down_install_dir закрепляет это ПОЗЖЕ,
        # ИСКЛЮЧАЯ data/ (её владелец — сервис-юзер). Намеренно НЕ делаем здесь
        # широкий `chown -R`: он захватил бы сохранённый data/ (scratch Claude) и
        # сломал бы права сервис-юзеру (ensure_data_dir чинит только *.db).
        log_ok "Release установлен в $INSTALL_DIR (без git, без токена)"
        return 0
    fi
    local repo_path_suffix="github.com/${REPO_OWNER}/${REPO_NAME}.git"
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        # Каталог принадлежит сервисному юзеру (chown в конце прошлого прогона), а
        # git здесь работает от root → git ≥ 2.35.2 ругается «detected dubious
        # ownership» и падает на первой же команде. Объявляем каталог доверенным,
        # иначе идемпотентный re-run обрывается на шаге Repository.
        git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
        local remote_url
        remote_url="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
        # Падаем только при НЕПУСТОМ и реально чужом remote (пустой = git ещё не
        # ответил — не повод печатать вводящее в заблуждение «different repository»).
        [[ -z "$remote_url" || "$remote_url" == *"$repo_path_suffix" ]] \
            || die "$INSTALL_DIR contains a different repository: $remote_url"
        # Refresh remote URL so git pull uses the current token (or none, if public).
        git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
        git -C "$INSTALL_DIR" fetch origin "$REPO_BRANCH"
        git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
        git -C "$INSTALL_DIR" pull --ff-only origin "$REPO_BRANCH"
        log_ok "Repository updated: $INSTALL_DIR"
    elif [[ -e "$INSTALL_DIR" && -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]]; then
        die "$INSTALL_DIR exists and is not empty. Remove it or set INSTALL_DIR=/srv/vels-claude."
    else
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
        log_ok "Repository cloned: $INSTALL_DIR"
    fi
    # Прод-копия не нуждается в тестах и дев-мусоре, а git clone/pull тянет ВСЁ
    # tracked-дерево (export-ignore из .gitattributes влияет только на git archive,
    # не на pull/clone). Срезаем их. CLAUDE.md/README/доки для пользователя остаются.
    rm -rf "$INSTALL_DIR/tests" "$INSTALL_DIR/web/src/__tests__" \
           "$INSTALL_DIR/web/.vite" "$INSTALL_DIR/web/dist_test" \
           "$INSTALL_DIR/.pytest_cache" 2>/dev/null || true
    # H-7: владельца НЕ отдаём сервис-юзеру. Все артефакты сборки (venv, фронт,
    # .env, config) создаём от root ниже по пайплайну, а в самом конце
    # lock_down_install_dir закрепляет root-владение + точечные права. Раньше
    # тут стоял chown -R на сервис-юзера → скомпрометированный Claude мог
    # переписать собственный код/venv и закрепиться.
    :
}

install_python_env() {
    step "Python environment"
    # H-7: venv создаётся и наполняется от ROOT (не сервис-юзером) — итог
    # принадлежит root и недоступен сервис-юзеру на запись. Сервис лишь
    # ИСПОЛНЯЕТ .venv/bin/python (read+exec), запись ему не нужна.
    # Проект требует Python >=3.11 (rpds-py в requirements.lock собран под 3.11).
    # НЕ используем system python3 вслепую: на Ubuntu 22.04 это 3.10 → pip падает
    # на rpds-py. Выбираем/доставляем подходящий интерпретатор и строим venv из него.
    if ! declare -F ensure_python311 >/dev/null 2>&1; then
        # Раздача одним файлом (curl сырого install.sh — см. README «Для
        # разработчиков»): scripts/lib/python-env.sh не было соседом на старте.
        # install_or_update_repo() уже наполнил $INSTALL_DIR полным деревом —
        # подхватываем хелпер оттуда.
        if [[ -f "$INSTALL_DIR/scripts/lib/python-env.sh" ]]; then
            # shellcheck source=/dev/null
            source "$INSTALL_DIR/scripts/lib/python-env.sh"
        fi
    fi
    declare -F ensure_python311 >/dev/null 2>&1 \
        || die "scripts/lib/python-env.sh не найден — не могу выбрать Python >=3.11."
    local pybin
    pybin="$(ensure_python311)"
    log_info "Python: $("$pybin" --version 2>&1) ($pybin)"
    # H-2: чиним и legacy-venv при ПОВТОРНОЙ установке (раньше пересоздавали
    # только когда .venv отсутствовал вовсе) — если существующий venv собран
    # на Python <3.11 (или сломан), пересоздаём его из выбранного интерпретатора.
    if [[ -d "$INSTALL_DIR/.venv" ]] \
        && ! "$INSTALL_DIR/.venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' >/dev/null 2>&1; then
        log_warn "Существующий venv собран на Python <3.11 (или повреждён) — пересоздаю из $pybin."
        rm -rf "$INSTALL_DIR/.venv"
    fi
    if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
        "$pybin" -m venv "$INSTALL_DIR/.venv"
    fi
    "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
    install_requirements_locked "$INSTALL_DIR/.venv/bin/pip"
    log_ok "Python dependencies installed"
}

install_requirements_locked() {
    # H-9: ставим из залоченного requirements.lock с проверкой хэшей
    # (--require-hashes — pip отвергает любой пакет, чей sha256 не совпал,
    # защита от подмены на зеркале/в supply-chain). Фоллбэк на requirements.txt
    # только если lock-файла нет (репо без него) — с предупреждением.
    local pip_bin=$1
    local lock="$INSTALL_DIR/requirements.lock"
    if [[ -f "$lock" ]]; then
        "$pip_bin" install --require-hashes --no-deps -r "$lock"
    else
        log_warn "requirements.lock отсутствует — ставлю из requirements.txt БЕЗ проверки хэшей."
        "$pip_bin" install -r "$INSTALL_DIR/requirements.txt"
    fi
}

write_env() {
    step ".env"
    local env_file="$INSTALL_DIR/.env"
    # При повторной установке сохраняем уже выданный JWT-секрет, чтобы не
    # разлогинить всех существующих пользователей.
    local jwt="$CFG_JWT_SECRET"
    local existing_jwt
    existing_jwt="$(existing_env_value WEB_JWT_SECRET "$env_file")"
    [[ -n "$existing_jwt" ]] && jwt="$existing_jwt"
    # CONNECTIONS_SECRET_KEY: тоже сохраняем существующий (ротация осиротит
    # зашифрованные секреты Connect Services — расшифровать станет нельзя).
    local conn_key="$CFG_CONNECTIONS_KEY"
    local existing_conn
    existing_conn="$(existing_env_value CONNECTIONS_SECRET_KEY "$env_file")"
    [[ -n "$existing_conn" ]] && conn_key="$existing_conn"
    touch "$env_file"
    # H-7: .env принадлежит root, права 0640 (root:<service-group>). systemd
    # читает EnvironmentFile от root ДО сброса привилегий; приложению
    # (pydantic-settings env_file=".env") .env нужен на ЧТЕНИЕ в рантайме —
    # поэтому группа сервис-юзера читает (0640), но ЗАПИСИ у сервис-юзера НЕТ.
    # Раньше .env был owned сервис-юзером (write) → скомпрометированный Claude мог
    # переписать токен/секреты/whitelist.
    chmod 600 "$env_file"
    chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$env_file"
    render_env_file "$CFG_TOKEN" "$CFG_IDS" "$CFG_PROJECTS_DIR" \
        "$jwt" "$CFG_BOT_USERNAME" "$CFG_ADMIN_LOGIN" "$CFG_ADMIN_PASSWORD" \
        "$CFG_ANTHROPIC_API_KEY" "$CFG_CLAUDE_OAUTH_TOKEN" "$CFG_WEB_PORT" "$CFG_PERSIST_DOMAIN" \
        "$conn_key" "$CFG_OWNER_USER_ID" >"$env_file"
    chmod 640 "$env_file"
    chown root:"${SERVICE_GROUP:-$SERVICE_USER}" "$env_file"
    log_ok ".env written (root:${SERVICE_GROUP:-$SERVICE_USER}, chmod 640 — read-only для сервис-юзера)"
}

ensure_projects_dir() {
    step "Projects directory"
    mkdir -p "$CFG_PROJECTS_DIR"
    chown "$SERVICE_USER":"${SERVICE_GROUP:-$SERVICE_USER}" "$CFG_PROJECTS_DIR"
    log_ok "Projects directory ready: $CFG_PROJECTS_DIR"
}

ensure_data_dir() {
    step "Data directory"
    # H-9-note: data/ хранит SQLite-БД (хэши паролей админов/юзеров) и scratch
    # (рабочий каталог Claude без проекта). Это ЕДИНСТВЕННЫЙ каталог под
    # INSTALL_DIR, куда сервис-юзеру нужна запись (см. ReadWritePaths в юните).
    # install -d -m 0700: только владелец (сервис-юзер) входит/читает/пишет —
    # БД с хэшами недоступна другим локальным пользователям.
    local data_dir="$INSTALL_DIR/data"
    install -d -m 0700 -o "$SERVICE_USER" -g "${SERVICE_GROUP:-$SERVICE_USER}" "$data_dir"
    # Существующие БД (повторная установка) — ужать права до 600.
    local db
    for db in "$data_dir"/*.db; do
        [[ -e "$db" ]] || continue
        chown "$SERVICE_USER":"${SERVICE_GROUP:-$SERVICE_USER}" "$db"
        chmod 600 "$db"
    done
    log_ok "Data directory ready (0700): $data_dir"
}

lock_down_install_dir() {
    step "Lock down install dir"
    # H-7 / H-9-note: финальная фиксация прав. Код, .venv, web/dist, config —
    # принадлежат root, сервис-юзеру доступны только на ЧТЕНИЕ/ИСПОЛНЕНИЕ.
    # data/ исключаем — он остаётся за сервис-юзером (запись Claude/БД).
    #  - chown -R root:<group> на всё, КРОМЕ data/ (data выставлен в ensure_data_dir);
    #  - корневой каталог 0750 (группа сервис-юзера входит и читает, посторонние — нет);
    #  - .env уже 0640 root:<group> (write_env) — chown ниже его не трогает (root уже владелец).
    local group="${SERVICE_GROUP:-$SERVICE_USER}"
    find "$INSTALL_DIR" -path "$INSTALL_DIR/data" -prune -o -print0 2>/dev/null \
        | xargs -0 chown -h root:"$group" 2>/dev/null || true
    chmod 0750 "$INSTALL_DIR"
    # .env — секреты, оставляем строго 0640 (xargs выше мог не тронуть права, только владельца).
    [[ -f "$INSTALL_DIR/.env" ]] && chmod 640 "$INSTALL_DIR/.env"
    log_ok "Код/venv принадлежат root (0750), сервис-юзер только читает; запись — лишь в data/"
}

install_systemd_unit() {
    step "systemd"
    render_systemd_unit >"$UNIT_PATH"
    chmod 644 "$UNIT_PATH"
    systemctl daemon-reload
    log_ok "Installed $UNIT_PATH"
}

ensure_caddy() {
    step "Caddy (reverse proxy)"
    if command -v caddy >/dev/null 2>&1; then
        log_ok "Caddy уже установлен: $(caddy version 2>/dev/null | head -n1)"
        return 0
    fi
    # НЕ фатально: при сбое (нет сети/репозитория) бот и веб (на 127.0.0.1) ставятся,
    # просто веб недоступен снаружи, пока Caddy не поставят. gpg --batch --yes —
    # идемпотентность (повторный запуск не падает на уже существующем keyring).
    if apt_install_missing debian-keyring debian-archive-keyring apt-transport-https gnupg curl \
        && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
        && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            > /etc/apt/sources.list.d/caddy-stable.list \
        && apt-get update -qq \
        && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy \
        && command -v caddy >/dev/null 2>&1; then
        log_ok "Caddy установлен: $(caddy version 2>/dev/null | head -n1)"
        return 0
    fi
    log_warn "Caddy не установился (сеть/репозиторий?). Бот и веб поднимутся, но веб НЕ будет"
    log_warn "доступен снаружи, пока не поставите Caddy: https://caddyserver.com/docs/install"
    return 0
}

write_caddy_config() {
    step "Caddy config"
    mkdir -p /etc/caddy
    render_caddyfile "$CFG_WEB_MODE" "$CFG_DOMAIN" "$CFG_WEB_PORT" >/etc/caddy/Caddyfile
    systemctl enable caddy >/dev/null 2>&1 || true
    if systemctl reload caddy >/dev/null 2>&1 || systemctl restart caddy >/dev/null 2>&1; then
        log_ok "Caddy сконфигурирован (режим: $CFG_WEB_MODE)"
    else
        log_warn "Caddy не перезапустился — проверьте: journalctl -u caddy -n 50"
    fi
}

probe_web_local() {
    # «active» у systemd ≠ «веб забиндил порт»: uvicorn стартует фоновой задачей,
    # и ошибка bind (порт занят / нет CAP_NET_BIND_SERVICE) НЕ роняет процесс (он
    # живёт на Telegram-поллинге). Активно щупаем порт — любой HTTP-ответ (даже 404)
    # значит, что порт реально слушается.
    local url="http://127.0.0.1:${CFG_WEB_PORT}/"
    local i
    for i in 1 2 3 4 5; do
        if curl -s --max-time 3 -o /dev/null "$url" 2>/dev/null; then
            CFG_WEB_REACHABLE=1
            log_ok "Веб слушает порт $CFG_WEB_PORT (локальная проба ответила)"
            return 0
        fi
        sleep 2
    done
    CFG_WEB_REACHABLE=0
    log_warn "Веб НЕ ответил на $url за ~10с — проверьте: journalctl -u $SERVICE_NAME -n 50"
}

probe_web_external() {
    # M-1: probe_web_local подтверждает только, что ПРИЛОЖЕНИЕ слушает локальный
    # порт — это НЕ значит, что снаружи всё доступно: Let's Encrypt мог ещё не
    # выпустить сертификат, а 80/443 нередко закрыты в облачной security group
    # (реальный инцидент). Проверяем best-effort сам публичный адрес.
    # Self-проба с этой же машины на СВОЙ публичный адрес подвержена
    # hairpin-NAT ограничениям некоторых облаков (ложный негатив даже когда
    # для реальных клиентов снаружи всё работает) — поэтому результат идёт в
    # финал как degraded-предупреждение, а НЕ как ошибка/провал установки.
    CFG_EXTERNAL_REACHABLE=0
    [[ "$CFG_WEB_MODE" == "domain" || "$CFG_WEB_MODE" == "ip_tls" ]] || return 0
    [[ -n "${CFG_PUBLIC_ORIGIN:-}" ]] || return 0
    local i
    for i in 1 2 3; do
        if curl -sk --max-time 5 -o /dev/null "$CFG_PUBLIC_ORIGIN" 2>/dev/null; then
            CFG_EXTERNAL_REACHABLE=1
            log_ok "Внешний адрес отвечает: $CFG_PUBLIC_ORIGIN"
            return 0
        fi
        (( i < 3 )) && sleep 3
    done
    log_warn "Внешний адрес $CFG_PUBLIC_ORIGIN не ответил (после $i попыток)."
    # Без пайпа в условии: grep -qi закрыл бы поток на первом совпадении,
    # journalctl поймал бы SIGPIPE, и pipefail превратил бы НАЙДЕННУЮ причину
    # сбоя в «причина не подтверждена» — диагностика врала бы ровно в тех
    # случаях, ради которых написана.
    local caddy_log=""
    command -v journalctl >/dev/null 2>&1 \
        && caddy_log="$(journalctl -u caddy -n 80 --no-pager 2>/dev/null || true)"
    if [[ -n "$caddy_log" ]] \
        && grep -qiE 'acme|obtain certificate|challenge failed|tls handshake error' <<<"$caddy_log"; then
        log_warn "В логах Caddy есть признаки ошибки ACME/сертификата: journalctl -u caddy -n 80"
    else
        log_warn "Причина не подтверждена по логам Caddy — либо hairpin-NAT провайдера мешает"
        log_warn "self-пробе с этой же машины, либо порты 80/443 закрыты в облачной security group."
    fi
}

start_service() {
    step "Start service"
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
    sleep 3
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        print_failure
    fi
    log_ok "$SERVICE_NAME is active"
    probe_web_local
}

print_failure() {
    log_err "$SERVICE_NAME did not become active."
    printf '\nRecent logs:\n'
    journalctl -u "$SERVICE_NAME" -n 30 --no-pager || true
    cat <<EOF

Next diagnostics:
  systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -n 100 --no-pager
  journalctl -u $SERVICE_NAME -f

EOF
    exit 1
}

print_success() {
    local web_line
    if [[ "${CFG_WEB_BUILT:-0}" != "1" ]]; then
        web_line="НЕ собран — веб-UI недоступен (см. лог сборки фронта выше; почините и перезапустите сервис)"
    elif [[ "${CFG_WEB_REACHABLE:-0}" != "1" ]]; then
        web_line="${CFG_PUBLIC_ORIGIN:-http://<server-ip>:$CFG_WEB_PORT}  (локально НЕ ответил — journalctl -u $SERVICE_NAME)"
    elif [[ ( "$CFG_WEB_MODE" == "domain" || "$CFG_WEB_MODE" == "ip_tls" ) \
            && "${CFG_EXTERNAL_REACHABLE:-0}" != "1" ]]; then
        # M-1: локальный порт слушает, но снаружи адрес не подтверждён — НЕ
        # зелёный успех, а degraded-предупреждение (install НЕ помечается failed).
        web_line="${CFG_PUBLIC_ORIGIN}  (выпуск TLS-сертификата НЕ подтверждён снаружи — откройте порты 80 и 443 в облачной security group, Caddy повторит выпуск автоматически; диагностика: journalctl -u caddy -n 80)"
    else
        web_line="${CFG_PUBLIC_ORIGIN:-http://<server-ip>:$CFG_WEB_PORT}"
    fi

    cat <<EOF

AI-Panel installation complete.

Service:        $SERVICE_NAME
Install path:   $INSTALL_DIR
Service user:   $SERVICE_USER
Projects dir:   $CFG_PROJECTS_DIR
Telegram bot:   ${CFG_BOT_USERNAME:+@}${CFG_BOT_USERNAME:-не настроен (интерфейс только веб)}
Claude auth:    $(claude_auth_status_human)

Web UI:         ${web_line}
Admin login:    ${CFG_ADMIN_LOGIN:-admin}
$( [[ "$CFG_ADMIN_PASSWORD_GENERATED" == 1 ]] && printf 'Admin password: %s   <-- СОХРАНИТЕ, показан один раз!' "$CFG_ADMIN_PASSWORD" )
EOF

    if [[ "${CLAUDE_AUTH_STATUS:-}" == "missing" ]]; then
        cat <<EOF

==> ТРЕБУЕТСЯ ДЕЙСТВИЕ: Claude ещё НЕ авторизован — бот не будет отвечать.
    Вариант A (рекомендуется, без браузера, оплата по API; ключ — console.anthropic.com/keys):
      echo 'ANTHROPIC_API_KEY=sk-ant-ВАШ_КЛЮЧ' | sudo tee -a $INSTALL_DIR/.env >/dev/null && sudo systemctl restart $SERVICE_NAME
    Вариант B (подписка Pro/Max) — авторизуйте сервисного пользователя один раз:
      $(claude_auth_command)
    затем: systemctl restart $SERVICE_NAME
EOF
    fi

    if [[ -n "${CFG_TOKEN:-}" ]]; then
        cat <<EOF

Telegram: откройте бота и отправьте /start.
EOF
    fi

    cat <<EOF

Web UI: открывается в браузере по адресу выше (логин/пароль администратора).
Снаружи проксирует Caddy (приложение слушает только 127.0.0.1). В облачной
security group откройте порты 80 и 443. Firewall (ufw) настроен: снаружи
открыты только SSH + 80/443, остальное закрыто.

$( if [[ "$CFG_WEB_MODE" == "domain" && "$CFG_DOMAIN" == *.sslip.io ]]; then \
       printf 'HTTPS без домена: %s — Caddy выпускает настоящий TLS-сертификат (Let'"'"'s Encrypt). Нужны открытые 80 и 443 + публичный IP.' "$CFG_DOMAIN"; \
   elif [[ "$CFG_WEB_MODE" == "domain" ]]; then \
       printf 'Домен: Caddy выпускает TLS-сертификат сам — домен должен указывать на IP сервера.'; \
   elif [[ "$CFG_WEB_MODE" == "ip_tls" ]]; then \
       printf 'Самоподписанный TLS: трафик шифрован, браузер один раз предупредит о сертификате (примите исключение).'; \
   else \
       printf 'ВНИМАНИЕ: по IP доступ идёт по http (БЕЗ шифрования) — пароли/сессии открыто. Это выбрано через WEB_MODE=ip. Для шифрования уберите WEB_MODE=ip (будет HTTPS через sslip.io).'; \
   fi )

Useful commands:
  systemctl status $SERVICE_NAME
  journalctl -u $SERVICE_NAME -f
  systemctl restart $SERVICE_NAME

EOF
}

main() {
    print_banner

    step "Preflight"
    check_os
    ensure_root
    resolve_service_user
    # Базовый python3 ставим для утилит/скриптов; venv строится не из него, а из
    # интерпретатора >=3.11, который выбирает/доставляет install_python_env
    # (ensure_python311) — system python3 на Ubuntu 22.04 = 3.10 и ломает pip.
    apt_install_missing curl git python3 python3-venv python3-pip bubblewrap
    if ! command -v bwrap >/dev/null 2>&1; then
        log_warn "bubblewrap (bwrap) не установлен — OS-джейл confined-сессий недоступен; держите claude.require_jail выключенным."
    fi
    ensure_node_runtime
    create_service_user_if_needed
    migrate_invoker_claude_session
    seed_claude_project_mcp_trust
    ensure_claude_cli
    resolve_claude_credentials
    verify_claude_auth

    # Веб — основной интерфейс, поэтому его параметры спрашиваются первыми;
    # Telegram идёт следом и может быть пропущен (prompt_onboarding).
    prompt_web_admin
    prompt_onboarding
    reconcile_projects_dir_with_user
    install_or_update_repo
    install_python_env
    build_frontend
    write_env
    write_web_config
    ensure_caddy
    write_caddy_config
    ensure_projects_dir
    ensure_data_dir
    install_systemd_unit
    open_firewall_port
    # H-7: ПОСЛЕ всех записей в INSTALL_DIR (код/venv/фронт/.env/config) и ПОСЛЕ
    # ensure_data_dir — закрепляем root-владение на всё, кроме data/. Должно идти
    # перед start_service, иначе сервис стартует с ещё «горячими» правами.
    lock_down_install_dir
    start_service
    # M-1: ПОСЛЕ open_firewall_port/start_service — иначе ufw/Caddy/приложение
    # ещё не готовы и проба всегда ложно "не отвечает" на свежей установке.
    probe_web_external
    print_success
}

(return 0 2>/dev/null) || main "$@"
