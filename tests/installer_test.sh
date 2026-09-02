#!/usr/bin/env bash
# Side-effect-free tests for scripts/install.sh helper functions.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/install.sh
# shellcheck disable=SC1091
source "$ROOT_DIR/scripts/install.sh"

FAIL=0

assert_eq() {
    local got=$1
    local want=$2
    local name=$3
    if [[ "$got" == "$want" ]]; then
        printf '  ok - %s\n' "$name"
    else
        printf '  not ok - %s\n    got:  %s\n    want: %s\n' "$name" "$got" "$want" >&2
        FAIL=1
    fi
}

assert_contains() {
    local haystack=$1
    local needle=$2
    local name=$3
    if [[ "$haystack" == *"$needle"* ]]; then
        printf '  ok - %s\n' "$name"
    else
        printf '  not ok - %s\n    missing: %s\n' "$name" "$needle" >&2
        FAIL=1
    fi
}

assert_fail() {
    local name=$1
    shift
    if "$@" >/dev/null 2>&1; then
        printf '  not ok - %s (expected failure)\n' "$name" >&2
        FAIL=1
    else
        printf '  ok - %s\n' "$name"
    fi
}

echo "== validate_token_format =="
assert_eq "$(validate_token_format '1234567890:AAFabcdefghijklmnopqrstuvwxyz12345')" "ok" "valid bot token"
assert_fail "empty token" validate_token_format ""
assert_fail "missing colon" validate_token_format "1234567890AAFabcdefghijklmnopqrstuvwxyz12345"
assert_fail "short secret" validate_token_format "1234567890:short"
assert_fail "non-numeric prefix" validate_token_format "abc:AAFabcdefghijklmnopqrstuvwxyz12345"

echo "== sanitize_input =="
# Очистка вставленного в терминал значения от артефактов, из-за которых живой
# ввод токена либо «висел», либо ложно считался неверным.
assert_eq "$(sanitize_input '123')" "123" "clean input is untouched"
assert_eq "$(sanitize_input '  1234567890:AAFabc  ')" "1234567890:AAFabc" "surrounding whitespace trimmed"
assert_eq "$(sanitize_input $'1234567890:AAFabc\r')" "1234567890:AAFabc" "trailing CR (CRLF paste) stripped"
assert_eq "$(sanitize_input $'\033[200~1234567890:AAFabc\033[201~')" "1234567890:AAFabc" "bracketed-paste markers stripped"
assert_eq "$(validate_token_format "$(sanitize_input $'\033[200~1234567890:AAFabcdefghijklmnopqrstuvwxyz12345\033[201~')")" "ok" "pasted token sanitizes to a valid token"
assert_eq "$(parse_user_ids "$(sanitize_input $'  123, 456 \r')")" "123,456" "pasted ids sanitize then parse"

echo "== getme_check (mock success contract) =="
# Живой цикл ввода токена полагается на контракт: при успехе getme_check печатает
# username в stdout и возвращает 0 (тогда цикл сохраняет токен и выходит).
assert_eq "$(VELS_GETME_MOCK=mybot getme_check '1234567890:AAFabc')" "mybot" "getme_check returns bot username on success"

echo "== parse_user_ids =="
assert_eq "$(parse_user_ids '123')" "123" "single user id"
assert_eq "$(parse_user_ids '123, 456 ,789')" "123,456,789" "whitespace is normalized"
assert_eq "$(parse_user_ids '123,123,456')" "123,456" "duplicates are removed"
assert_fail "empty ids" parse_user_ids ""
assert_fail "non numeric id" parse_user_ids "123,abc"
assert_fail "negative id" parse_user_ids "-1"
assert_fail "trailing comma rejected" parse_user_ids "123,"
assert_fail "internal whitespace rejected" parse_user_ids "123 456"
assert_fail "internal whitespace in list rejected" parse_user_ids "123, 45 6"
assert_fail "newline-separated ids rejected" parse_user_ids $'123\n456'

echo "== expand_absolute_path =="
assert_eq "$(HOME=/home/vels expand_absolute_path '~/projects')" "/home/vels/projects" "tilde expands"
assert_eq "$(SERVICE_HOME=/home/alice HOME=/root expand_absolute_path '~/projects')" "/home/alice/projects" "tilde uses service home when set"
assert_eq "$(HOME=/root expand_absolute_path '~/projects' /home/bob)" "/home/bob/projects" "tilde uses explicit home when provided"
assert_eq "$(expand_absolute_path '/srv/vels-claude/')" "/srv/vels-claude" "trailing slash stripped"
assert_eq "$(expand_absolute_path '/')" "/" "root slash preserved"
assert_fail "relative path rejected" expand_absolute_path "projects"

echo "== resolve_service_user =="
# M-2: дефолт для euid=0 теперь — выделенный vels-bot, ДАЖЕ если задан
# SUDO_USER (личный логин-аккаунт на облачных VPS обычно имеет NOPASSWD sudo —
# отдавать его боту с bypassPermissions расширяет поверхность атаки).
VELS_EUID_OVERRIDE=0 SUDO_USER=vels-missing-sudo-user VELS_PASSWD_ENTRY_OVERRIDE= VELS_GROUP_ENTRY_OVERRIDE= VELS_UNIT_CONTENT_OVERRIDE= resolve_service_user
assert_eq "$SERVICE_USER" "vels-bot" "sudo path now defaults to dedicated vels-bot (not login user)"
assert_eq "$SERVICE_HOME" "/var/lib/vels-bot" "sudo path uses vels-bot home by default"
assert_eq "$SERVICE_GROUP" "vels-bot" "sudo path uses vels-bot group by default"
assert_eq "$SERVICE_NEEDS_CREATE" "1" "sudo path creates vels-bot when missing"

# Opt-in: VELS_USE_LOGIN_USER=1 явно возвращает старое поведение (SUDO_USER).
VELS_EUID_OVERRIDE=0 SUDO_USER=vels-missing-sudo-user VELS_USE_LOGIN_USER=1 VELS_PASSWD_ENTRY_OVERRIDE= VELS_GROUP_ENTRY_OVERRIDE= VELS_UNIT_CONTENT_OVERRIDE= resolve_service_user
assert_eq "$SERVICE_USER" "vels-missing-sudo-user" "VELS_USE_LOGIN_USER=1 opts back into SUDO_USER"
assert_eq "$SERVICE_HOME" "/home/vels-missing-sudo-user" "opt-in sudo path home fallback"
assert_eq "$SERVICE_GROUP" "vels-missing-sudo-user" "opt-in sudo path group fallback"
assert_eq "$SERVICE_NEEDS_CREATE" "0" "opt-in sudo path does not create user"
unset VELS_USE_LOGIN_USER

# Уже установленный юнит (User=<логин-юзер> с самого первого install до M-2) —
# повторный запуск НЕ переключает сервис-юзера, иначе сломает владение data/.venv.
VELS_EUID_OVERRIDE=0 SUDO_USER=alice VELS_UNIT_CONTENT_OVERRIDE=$'[Service]\nUser=alice\n' VELS_PASSWD_ENTRY_OVERRIDE="alice:x:1000:1000::/home/alice:/bin/bash" VELS_GROUP_ENTRY_OVERRIDE="alice:x:1000:" resolve_service_user
assert_eq "$SERVICE_USER" "alice" "existing unit User= is preserved across re-run"
assert_eq "$SERVICE_HOME" "/home/alice" "existing unit user home resolved"
assert_eq "$SERVICE_NEEDS_CREATE" "0" "existing unit user does not need creation"
unset VELS_UNIT_CONTENT_OVERRIDE

unset SUDO_USER
VELS_EUID_OVERRIDE=0 VELS_PASSWD_ENTRY_OVERRIDE="vels-bot:x:999:998::/srv/existing-vels:/usr/sbin/nologin" VELS_GROUP_ENTRY_OVERRIDE="vels-existing:x:998:" VELS_UNIT_CONTENT_OVERRIDE= resolve_service_user
assert_eq "$SERVICE_USER" "vels-bot" "direct root uses existing managed user"
assert_eq "$SERVICE_HOME" "/srv/existing-vels" "existing managed user home"
assert_eq "$SERVICE_GROUP" "vels-existing" "existing managed user group"
assert_eq "$SERVICE_NEEDS_CREATE" "0" "existing managed user does not need creation"

VELS_EUID_OVERRIDE=0 VELS_PASSWD_ENTRY_OVERRIDE= VELS_GROUP_ENTRY_OVERRIDE= VELS_UNIT_CONTENT_OVERRIDE= resolve_service_user
assert_eq "$SERVICE_USER" "vels-bot" "direct root uses managed user fallback"
assert_eq "$SERVICE_HOME" "/var/lib/vels-bot" "missing managed user home fallback"
assert_eq "$SERVICE_GROUP" "vels-bot" "missing managed user group fallback"
assert_eq "$SERVICE_NEEDS_CREATE" "1" "missing managed user needs creation"

assert_fail "plain non-root rejected" env -i HOME="$HOME" VELS_EUID_OVERRIDE=1000 bash -c "source '$ROOT_DIR/scripts/install.sh'; resolve_service_user"
assert_fail "non-root sudo user rejected" env -i HOME="$HOME" SUDO_USER=alice VELS_EUID_OVERRIDE=1000 bash -c "source '$ROOT_DIR/scripts/install.sh'; resolve_service_user"

echo "== reconcile_projects_dir =="
SERVICE_USER="vels-bot"
SERVICE_HOME="/var/lib/vels-bot"
SERVICE_NEEDS_CREATE=1
CFG_PROJECTS_DIR="/root/projects"
reconcile_projects_dir_with_user
assert_eq "$CFG_PROJECTS_DIR" "/var/lib/vels-bot/projects" "managed user projects dir is moved from root"

SERVICE_USER="vels-bot"
SERVICE_HOME="/srv/existing-vels"
SERVICE_NEEDS_CREATE=0
CFG_PROJECTS_DIR="/root/projects"
reconcile_projects_dir_with_user
assert_eq "$CFG_PROJECTS_DIR" "/srv/existing-vels/projects" "existing managed user projects dir is moved from root"

SERVICE_USER="alice"
SERVICE_HOME="/home/alice"
SERVICE_NEEDS_CREATE=0
CFG_PROJECTS_DIR="/home/alice/projects"
reconcile_projects_dir_with_user
assert_eq "$CFG_PROJECTS_DIR" "/home/alice/projects" "normal sudo user projects dir preserved"

echo "== render_env_file =="
env_content="$(render_env_file '123:ABC' '111,222' '/home/alice/projects')"
assert_contains "$env_content" "TELEGRAM_BOT_TOKEN=123:ABC" "env token"
assert_contains "$env_content" "ALLOWED_USER_IDS=111,222" "env ids"
assert_contains "$env_content" "PROJECTS_DIR=/home/alice/projects" "env projects"
assert_contains "$env_content" "SESSION_DATABASE_PATH=data/sessions.db" "env sqlite path"

echo "== render_env_file (web) =="
web_env="$(render_env_file '123:ABC' '111,222' '/home/alice/projects' 'deadbeefsecret' 'mybot' 'admin' 'pw123456')"
assert_contains "$web_env" "TELEGRAM_BOT_TOKEN=123:ABC" "web env keeps base token"
assert_contains "$web_env" "WEB_JWT_SECRET=deadbeefsecret" "web env jwt secret"
assert_contains "$web_env" "TELEGRAM_BOT_USERNAME=mybot" "web env bot username"
assert_contains "$web_env" "ADMIN_LOGIN=admin" "web env admin login"
assert_contains "$web_env" "ADMIN_PASSWORD=pw123456" "web env admin password"
# Без web-аргументов (обратная совместимость): web-строк быть не должно.
base_only="$(render_env_file '123:ABC' '111,222' '/home/alice/projects')"
assert_fail "no jwt line without web args" grep -q "WEB_JWT_SECRET" <<<"$base_only"

echo "== render_env_file (claude creds + web port) =="
# ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN / WEB_PORT — опциональные хвостовые
# аргументы (8/9/10). Пишутся в .env только когда заданы (zero-touch Claude + идемпотентный порт).
creds_env="$(render_env_file '123:ABC' '111,222' '/home/alice/projects' 'jwt' 'mybot' 'admin' 'pw123456' 'sk-ant-xyz' '' '80')"
assert_contains "$creds_env" "ANTHROPIC_API_KEY=sk-ant-xyz" "env anthropic api key"
assert_contains "$creds_env" "WEB_PORT=80" "env web port persisted"
assert_fail "no oauth token line when empty" grep -q "CLAUDE_CODE_OAUTH_TOKEN" <<<"$creds_env"
token_env="$(render_env_file '123:ABC' '111,222' '/p' '' '' '' '' '' 'oauth-tok' '8765')"
assert_contains "$token_env" "CLAUDE_CODE_OAUTH_TOKEN=oauth-tok" "env claude oauth token"
assert_contains "$token_env" "WEB_PORT=8765" "env web port 8765"
assert_fail "no anthropic key line when empty" grep -q "ANTHROPIC_API_KEY" <<<"$token_env"
dom_env="$(render_env_file '123:ABC' '1' '/p' '' '' '' '' '' '' '8765' 'claude.example.com')"
assert_contains "$dom_env" "WEB_DOMAIN=claude.example.com" "env web domain persisted"
assert_fail "no web domain without arg" grep -q "WEB_DOMAIN" <<<"$base_only"
# Без хвостовых аргументов — ни creds, ни WEB_PORT.
assert_fail "no anthropic key without creds args" grep -q "ANTHROPIC_API_KEY" <<<"$base_only"
assert_fail "no web port without creds args" grep -q "WEB_PORT" <<<"$base_only"

echo "== gen_connections_key =="
# Fernet-ключ для Connect Services: url-safe base64 из 32 случайных байт.
key="$(gen_connections_key)"
case "$key" in *[!A-Za-z0-9_=-]*) echo "FAIL: connections key not url-safe base64"; FAIL=1;; *) echo "  ok - connections key is url-safe base64";; esac
[ "${#key}" -ge 40 ] || { echo "FAIL: connections key too short"; FAIL=1; }
[ "${#key}" -ge 40 ] && echo "  ok - connections key length >= 40"

echo "== render_env_file (connections key) =="
# CONNECTIONS_SECRET_KEY — новый хвостовой опциональный аргумент (12).
conn_env="$(render_env_file '123:ABC' '111,222' '/p' '' '' '' '' '' '' '8765' '' 'fernetkey123')"
assert_contains "$conn_env" "CONNECTIONS_SECRET_KEY=fernetkey123" "env connections secret key"
assert_fail "no connections key line without arg" grep -q "CONNECTIONS_SECRET_KEY" <<<"$base_only"

echo "== render_web_config =="
web_cfg="$(render_web_config 'https://claude.example.com')"
assert_contains "$web_cfg" "enabled: true" "web config enabled"
assert_contains "$web_cfg" "webhooks:" "web config writes webhooks block"
assert_contains "$web_cfg" "enabled: false" "web config disables webhook (defense-in-depth)"
assert_contains "$web_cfg" 'public_origin: "https://claude.example.com"' "web config public_origin"
# Слушаем локально (наружу — через Caddy reverse proxy).
assert_contains "$web_cfg" 'host: "127.0.0.1"' "web config binds loopback (Caddy fronts)"
ip_cfg="$(render_web_config 'http://1.2.3.4:8765' '127.0.0.1' '8765')"
assert_contains "$ip_cfg" 'host: "127.0.0.1"' "web config explicit host"
assert_contains "$ip_cfg" 'public_origin: "http://1.2.3.4:8765"' "web config ip origin"
assert_contains "$ip_cfg" 'port: 8765' "web config explicit port"
# По умолчанию внутренний порт 8765 (наружу :80/:443 — Caddy).
assert_contains "$web_cfg" 'port: 8765' "web config default internal port 8765"

echo "== render_systemd_unit =="
unit="$(SERVICE_USER=alice SERVICE_GROUP=alice SERVICE_HOME=/home/alice INSTALL_DIR=/opt/vels-claude SERVICE_NAME=vels-claude render_systemd_unit)"
assert_contains "$unit" "Description=Vels Claude (Web)" "unit description (web-only без токена)"
assert_contains "$unit" "User=alice" "unit user"
assert_contains "$unit" "Group=alice" "unit group"
assert_contains "$unit" "WorkingDirectory=/opt/vels-claude" "unit working dir"
assert_contains "$unit" "EnvironmentFile=/opt/vels-claude/.env" "unit env file"
assert_contains "$unit" "ExecStart=/opt/vels-claude/.venv/bin/python /opt/vels-claude/scripts/run_web.py" "unit exec (web-only без токена)"
bot_unit="$(SERVICE_USER=alice SERVICE_GROUP=alice SERVICE_HOME=/home/alice INSTALL_DIR=/opt/vels-claude SERVICE_NAME=vels-claude CFG_TOKEN=123456:AAFabc render_systemd_unit)"
assert_contains "$bot_unit" "Description=Vels Claude (Telegram + Web)" "unit description (с токеном)"
assert_contains "$bot_unit" "ExecStart=/opt/vels-claude/.venv/bin/python -m src.main" "unit exec (с токеном)"
assert_contains "$unit" "Environment=HOME=/home/alice" "unit home"
assert_fail "unit has no privileged-port capability (Caddy fronts :80)" grep -q "AmbientCapabilities" <<<"$unit"
assert_contains "$unit" "ReadWritePaths=/opt/vels-claude/data /home/alice /home/alice/projects" "unit write paths"
assert_contains "$unit" "NoNewPrivileges=true" "unit has NoNewPrivileges hardening"
assert_contains "$unit" "ProtectSystem=strict" "unit has ProtectSystem hardening"
assert_contains "$unit" "PrivateTmp=true" "unit has PrivateTmp hardening"
assert_eq "$(SERVICE_USER=alice SERVICE_HOME=/home/alice claude_probe_command)" "sudo -u alice -H env HOME=/home/alice claude -p ping --output-format stream-json --verbose" "claude probe validates auth as service user"

custom_unit="$(SERVICE_USER=alice SERVICE_GROUP=alice SERVICE_HOME=/home/alice INSTALL_DIR=/srv/vels-claude SERVICE_NAME=vels-claude CFG_PROJECTS_DIR=/srv/projects render_systemd_unit)"
assert_contains "$custom_unit" "WorkingDirectory=/srv/vels-claude" "custom install dir in unit"
assert_contains "$custom_unit" "EnvironmentFile=/srv/vels-claude/.env" "custom env path in unit"
assert_contains "$custom_unit" "ReadWritePaths=/srv/vels-claude/data /home/alice /srv/projects" "custom project dir in write paths"

echo "== validate_domain_format =="
assert_eq "$(validate_domain_format 'claude.example.com')" "ok" "valid domain"
assert_fail "empty domain" validate_domain_format ""
assert_fail "scheme rejected" validate_domain_format "https://x.com"
assert_fail "path rejected" validate_domain_format "x.com/agent"
assert_fail "space rejected" validate_domain_format "x .com"
assert_fail "no dot rejected" validate_domain_format "localhost"
assert_fail "bare ipv4 rejected (no ACME on IP)" validate_domain_format "1.2.3.4"

echo "== render_caddyfile =="
ip_caddy="$(render_caddyfile ip '' 8765)"
assert_contains "$ip_caddy" ":80 {" "ip caddy listens on :80"
assert_contains "$ip_caddy" "handle_path /agent/* {" "ip caddy strips /agent prefix"
assert_contains "$ip_caddy" "reverse_proxy 127.0.0.1:8765" "ip caddy proxies internal port"
assert_contains "$ip_caddy" 'X-Content-Type-Options "nosniff"' "ip caddy has security headers"
assert_fail "ip caddy has no HSTS (no TLS)" grep -q "Strict-Transport-Security" <<<"$ip_caddy"
dom_caddy="$(render_caddyfile domain claude.example.com 8765)"
assert_contains "$dom_caddy" "claude.example.com {" "domain caddy uses domain block (auto-HTTPS)"
assert_contains "$dom_caddy" "reverse_proxy 127.0.0.1:8765" "domain caddy proxies internal port"
assert_fail "domain caddy has no /agent strip" grep -q "handle_path" <<<"$dom_caddy"
assert_contains "$dom_caddy" "Strict-Transport-Security" "domain caddy sends HSTS"
assert_contains "$dom_caddy" 'X-Frame-Options "DENY"' "domain caddy anti-clickjacking"
# ip_tls — самоподписанный TLS на голый IP.
iptls_caddy="$(render_caddyfile ip_tls 1.2.3.4 8765)"
assert_contains "$iptls_caddy" "https://1.2.3.4 {" "ip_tls caddy binds https on bare ip"
assert_contains "$iptls_caddy" "tls internal" "ip_tls caddy uses internal CA"
assert_contains "$iptls_caddy" "reverse_proxy 127.0.0.1:8765" "ip_tls caddy proxies internal port"

echo "== build_ip_origin / resolve_web_mode =="
assert_eq "$(build_ip_origin '1.2.3.4')" "http://1.2.3.4/agent" "ip origin has /agent path"
CFG_DOMAIN=""; CFG_WEB_MODE=""; CFG_VITE_BASE=""; CFG_PUBLIC_ORIGIN=""
DOMAIN="claude.example.com" PUBLIC_ORIGIN="" resolve_web_mode "9.9.9.9"
assert_eq "$CFG_WEB_MODE" "domain" "env DOMAIN -> domain mode"
assert_eq "$CFG_PUBLIC_ORIGIN" "https://claude.example.com" "domain mode origin is https"
assert_eq "$CFG_VITE_BASE" "/" "domain mode vite base is root"
# Без домена → HTTPS через <ip>.sslip.io (настоящий сертификат, без покупки домена).
CFG_DOMAIN=""; CFG_WEB_MODE=""; CFG_VITE_BASE=""; CFG_PUBLIC_ORIGIN=""; CFG_PERSIST_DOMAIN=x
DOMAIN="" PUBLIC_ORIGIN="" WEB_MODE="" WEB_TLS="" resolve_web_mode "9.9.9.9"
assert_eq "$CFG_WEB_MODE" "domain" "no domain -> sslip.io HTTPS (domain mode)"
assert_eq "$CFG_PUBLIC_ORIGIN" "https://9.9.9.9.sslip.io" "sslip origin is https"
assert_eq "$CFG_VITE_BASE" "/" "sslip mode vite base is root"
assert_eq "$CFG_PERSIST_DOMAIN" "" "auto sslip domain is NOT persisted to .env"
# WEB_TLS=internal → самоподписанный TLS на голый IP.
CFG_DOMAIN=""; CFG_WEB_MODE=""; CFG_VITE_BASE=""; CFG_PUBLIC_ORIGIN=""
WEB_MODE="" DOMAIN="" PUBLIC_ORIGIN="" WEB_TLS=internal resolve_web_mode "9.9.9.9"
assert_eq "$CFG_WEB_MODE" "ip_tls" "WEB_TLS=internal -> ip_tls mode"
assert_eq "$CFG_PUBLIC_ORIGIN" "https://9.9.9.9" "ip_tls origin is https bare ip"
assert_eq "$CFG_VITE_BASE" "/" "ip_tls vite base is root"
# WEB_MODE=ip → явный небезопасный http по IP (opt-out из шифрования).
CFG_DOMAIN=""; CFG_WEB_MODE=""; CFG_VITE_BASE=""; CFG_PUBLIC_ORIGIN=""
WEB_MODE=ip DOMAIN="" PUBLIC_ORIGIN="" WEB_TLS="" resolve_web_mode "9.9.9.9"
assert_eq "$CFG_WEB_MODE" "ip" "WEB_MODE=ip -> plain http ip mode"
assert_eq "$CFG_PUBLIC_ORIGIN" "http://9.9.9.9/agent" "WEB_MODE=ip origin http /agent"
assert_eq "$CFG_VITE_BASE" "/agent/" "WEB_MODE=ip vite base /agent/"
# WEB_MODE=ip — явный IP даже при заданном DOMAIN
CFG_DOMAIN=x; CFG_WEB_MODE=x; CFG_VITE_BASE=x; CFG_PUBLIC_ORIGIN=x
WEB_MODE=ip DOMAIN="claude.example.com" PUBLIC_ORIGIN="" resolve_web_mode "9.9.9.9"
assert_eq "$CFG_WEB_MODE" "ip" "WEB_MODE=ip forces ip despite DOMAIN"
assert_eq "$CFG_PUBLIC_ORIGIN" "http://9.9.9.9/agent" "WEB_MODE=ip ip origin"
# Непустой не-https PUBLIC_ORIGIN — уважается как есть, не затирается build_ip_origin
CFG_DOMAIN=x; CFG_WEB_MODE=x; CFG_VITE_BASE=x; CFG_PUBLIC_ORIGIN=x
WEB_MODE="" DOMAIN="" PUBLIC_ORIGIN="http://myhost:9000/agent" resolve_web_mode "9.9.9.9"
assert_eq "$CFG_WEB_MODE" "ip" "custom origin -> ip mode"
assert_eq "$CFG_PUBLIC_ORIGIN" "http://myhost:9000/agent" "custom origin honored verbatim"
assert_eq "$CFG_VITE_BASE" "/agent/" "custom /agent origin -> base /agent/"
# https://<голый ip> не уходит в домен-режим (иначе обречённый ACME)
CFG_DOMAIN=x; CFG_WEB_MODE=x; CFG_VITE_BASE=x; CFG_PUBLIC_ORIGIN=x
WEB_MODE="" DOMAIN="" PUBLIC_ORIGIN="https://1.2.3.4" resolve_web_mode "9.9.9.9"
assert_eq "$CFG_WEB_MODE" "ip" "https bare-ip is not domain mode"

echo "== installer constants =="
assert_eq "$SERVICE_NAME" "vels-claude" "default service name"
assert_eq "$INSTALL_DIR" "/opt/vels-claude" "default install dir"
# Light раздаётся из ПУБЛИЧНОГО репозитория: git-путь установщика должен вести
# туда, иначе клон без токена упирается в приватный Vels-Claude.
assert_eq "$REPO_NAME" "vels-claude-light" "default repo name matches public light repo"
assert_contains "$REPO_URL" "github.com/nick-vels/vels-claude-light.git" "repo url points at public light repo"

echo "== claude auth command =="
SERVICE_USER="alice"
SERVICE_HOME="/home/alice"
assert_eq "$(claude_auth_command)" "sudo -u alice -H env HOME=/home/alice claude" "auth command uses service user home"
assert_eq "$(claude_probe_command)" "sudo -u alice -H env HOME=/home/alice claude -p ping --output-format stream-json --verbose" "probe command is documented with service home"

echo "== apt не уходит в интерактив =="
# needrestart на Ubuntu 22.04+ после установки пакетов открывает диалог
# «Which services should be restarted?» и ждёт ввода. Установщик приходит
# бутстрапом (`curl … | sudo bash`), где stdin — пайп от curl: отвечать
# некому, установка виснет насмерть. Прогон на живом сервере встал ровно так.
assert_eq "${NEEDRESTART_MODE:-}" "a" "NEEDRESTART_MODE=a выставлен"
assert_eq "${DEBIAN_FRONTEND:-}" "noninteractive" "DEBIAN_FRONTEND=noninteractive выставлен"
# Экспорт, а не просто присваивание: apt дёргают и вложенные скрипты
# (setup_20.x от NodeSource) — их окружение тоже должно быть неинтерактивным.
assert_contains "$(grep -E '^export (NEEDRESTART_MODE|DEBIAN_FRONTEND)=' "$ROOT_DIR/scripts/install.sh")"     "export NEEDRESTART_MODE=a" "флаги именно экспортированы"

echo "== node ставится только из NodeSource =="
# Скрипт NodeSource может вернуть 0, не добавив репозиторий, — тогда apt молча
# поставит nodejs 12 из штатного репозитория Ubuntu, вообще без npm.
#
# Проверяем ПОВЕДЕНИЕ, а не наличие строки. Первая версия этой проверки была
# написана как "apt-cache policy nodejs | grep -q nodesource" и валила установку
# именно тогда, когда репозиторий НАЙДЕН: grep -q выходит на первом совпадении и
# закрывает пайп, apt-cache ловит SIGPIPE и отдаёт 141, а set -o pipefail берёт
# худший код в пайпе. Ассерт «строка на месте» такую дыру не видит — строка была
# на месте. Ловится только вызовом функции на реалистичном выводе, где после
# совпадения идёт ещё много строк.
apt-cache() {
    printf 'nodejs:\n  Installed: (none)\n  Candidate: 20.20.2-1nodesource1\n  Version table:\n'
    printf '     20.20.2-1nodesource1 500\n        500 https://deb.nodesource.com/node_20.x nodistro/main amd64 Packages\n'
    local i
    for i in $(seq 1 2000); do
        printf '     12.22.9~dfsg-1ubuntu3.6 500\n        500 http://archive.ubuntu.com/ubuntu jammy/universe amd64 Packages %s\n' "$i"
    done
}
if nodesource_repo_present; then
    printf '  ok - %s\n' "репозиторий найден => проверка НЕ падает (нет SIGPIPE под pipefail)"
else
    printf '  not ok - %s\n    got exit: %s\n' "репозиторий найден => проверка НЕ падает" "$?" >&2
    FAIL=1
fi
# Обратный случай: репозитория нет — проверка обязана вернуть неуспех.
apt-cache() { printf 'nodejs:\n  Candidate: 12.22.9~dfsg-1ubuntu3.6\n'; }
assert_fail "репозитория нет => проверка сигналит неуспех" nodesource_repo_present
unset -f apt-cache

echo "== токен из окружения без ID не зацикливает установку =="
# Прогон на живом сервере: TELEGRAM_BOT_TOKEN передан окружением, ALLOWED_USER_IDS
# нет — установщик спросил Telegram user ID, получил пустой ввод, напечатал
# ошибку и спросил снова. И так по кругу, с таймаутом read в 900 секунд на
# итерацию. Отвечать в скриптовой установке некому: вопрос задан тому, кого нет.
#
# Проверяем поведением: prompt_onboarding в этом режиме обязана упасть через
# die, а не уйти в цикл. Внешние вызовы (getMe, чтение .env) замоканы, чтобы
# тест не ходил в сеть и не зависел от состояния машины.
_ids_probe="$(
    TELEGRAM_BOT_TOKEN='123456:AAFabcdefghijklmnopqrstuvwxyz1234567' bash -c '
        set -uo pipefail
        source "'"$ROOT_DIR"'/scripts/install.sh"
        getme_check() { printf "Velstestbot"; }
        existing_env_value() { printf ""; }
        die() { printf "DIED: %s\n" "$1"; exit 42; }
        log_info() { :; }; log_ok() { :; }; log_err() { :; }
        prompt_onboarding
        printf "NO_DIE\n"
    ' 2>/dev/null || true
)"
case "$_ids_probe" in
    *DIED*задан*через*окружение*)
        printf '  ok - %s\n' "токен из окружения без ALLOWED_USER_IDS => падаем сразу, с подсказкой" ;;
    *NO_DIE*)
        printf '  not ok - %s\n    установщик не упал: ушёл бы в цикл вопроса\n' \
            "токен из окружения без ALLOWED_USER_IDS => падаем сразу" >&2
        FAIL=1 ;;
    *)
        printf '  not ok - %s\n    неожиданный вывод: %s\n' \
            "токен из окружения без ALLOWED_USER_IDS => падаем сразу" "$_ids_probe" >&2
        FAIL=1 ;;
esac

# Интерактивный путь тоже не должен быть бесконечным: там пустой Enter — не
# пропуск (как у токена и домена), а ошибка ввода, поэтому нужен счётчик.
assert_contains "$(grep -c 'id_attempts' "$ROOT_DIR/scripts/install.sh")" "4" \
    "у вопроса про ID есть счётчик попыток"


echo "== uninstall: home с проектами внутри не сносится =="
# Прогон на живом сервере: uninstall.sh печатал «НЕ будет удалено: папка
# проектов /var/lib/vels-bot/projects» и удалял её. Дефолтный PROJECTS_DIR лежит
# внутри home сервис-юзера, а `userdel -r` сносит home целиком. Защита в скрипте
# была, но проверяла другой случай — «проекты внутри INSTALL_DIR».
#
# Файл-маркер «МОЙ КОД, ПОТЕРЯ = БАГ» после удаления исчезал. Для участника это
# значит потерю всего, что он писал через Vels Claude, — молча и с обещанием
# обратного на экране.
#
# uninstall.sh source-безопасен (guard `(return 0) || main`), поэтому дёргаем
# решающую функцию напрямую: сквозной прогон требует root и systemd, а вот
# правило «внутри home или нет» проверяется здесь и сейчас.
(
    source "$ROOT_DIR/scripts/uninstall.sh"

    _case() {  # ожидание, проекты, home, имя
        local want=$1 projects=$2 home=$3 name=$4
        if projects_inside_home "$projects" "$home"; then got=inside; else got=outside; fi
        if [[ "$got" == "$want" ]]; then
            printf '  ok - %s\n' "$name"
        else
            printf '  not ok - %s\n    ждали: %s, получили: %s\n' "$name" "$want" "$got" >&2
            exit 1
        fi
    }

    _case inside  "/var/lib/vels-bot/projects" "/var/lib/vels-bot" \
        "дефолтный PROJECTS_DIR внутри home => home сохраняем"
    _case outside "/srv/projects" "/var/lib/vels-bot" \
        "проекты вне home => home можно удалять"
    # Ловушка на префиксе: /var/lib/vels-bot-other НЕ внутри /var/lib/vels-bot.
    _case outside "/var/lib/vels-bot-other/projects" "/var/lib/vels-bot" \
        "похожее имя каталога не считается вложенностью"
    # home=/ у сломанного passwd не должен объявлять «внутри» весь диск.
    _case outside "/anything" "/" \
        "home=/ не делает всё содержимое диска защищённым"
) || FAIL=1

# Ветка userdel без -r должна существовать: при сохранении home удалять его
# нельзя, иначе фикс бессмыслен.
assert_contains "$(grep -c 'userdel "\$service_user"' "$ROOT_DIR/scripts/uninstall.sh")" "1" \
    "есть удаление пользователя без -r (home остаётся)"


echo "== python: release candidate не годится =="
# В universe Ubuntu 22.04 под именем python3.11 лежит 3.11.0~rc1 — release
# candidate от октября 2022, без патчей безопасности. Он проходит проверку
# version_info >= (3, 11), после чего ensure_python311 считает задачу решённой
# и до deadsnakes с настоящим релизом дело не доходит. Прогон на живом сервере
# показал именно это: в проде оказался Python 3.11.0rc1.
#
# Заглушка-RC отвечает как этот интерпретатор: на вопрос о версии — да, на
# вопрос про releaselevel — нет. Старая проверка (без releaselevel) её
# принимала. PATH подменяем только на время вызова, иначе тест теряет cat/chmod.
_probe_rc="$(mktemp -d)"
_probe_final="$(mktemp -d)"
cat > "$_probe_rc/python3.11" <<'PROBE'
#!/bin/sh
case "$*" in
  *releaselevel*) exit 1 ;;
  *) exit 0 ;;
esac
PROBE
cat > "$_probe_final/python3.11" <<'PROBE'
#!/bin/sh
exit 0
PROBE
chmod +x "$_probe_rc/python3.11" "$_probe_final/python3.11"

if picked="$(PATH="$_probe_rc" pick_python 2>/dev/null)"; then
    printf '  not ok - %s\n    выбран: %s\n' "release candidate отвергается" "$picked" >&2
    FAIL=1
else
    printf '  ok - %s\n' "release candidate отвергается (до deadsnakes дойдёт)"
fi

# Тот же интерпретатор, но final — обязан быть принят, иначе проверка просто
# запрещает всё подряд.
if PATH="$_probe_final" pick_python >/dev/null 2>&1; then
    printf '  ok - %s\n' "final-интерпретатор принимается"
else
    printf '  not ok - %s\n' "final-интерпретатор принимается" >&2
    FAIL=1
fi
rm -rf "$_probe_rc" "$_probe_final"


echo "== интерактивные prompt'ы видны =="
# `read -p` печатает приглашение в stderr. У вопроса про Telegram-токен stderr
# уходит в /dev/null (чтобы не пугать сообщением об отсутствии tty), и вместе с
# ним пропадала сама строка «Token (Enter — пропустить)»: человек видел пустой
# курсор и не понимал, чего от него ждут. Приглашение печатаем отдельным printf.
assert_fail "prompt не печатается через read -p с заглушённым stderr"     grep -qE "read .*-p .*2>/dev/null" "$ROOT_DIR/scripts/install.sh"

exit "$FAIL"
