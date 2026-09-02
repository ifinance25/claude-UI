#!/usr/bin/env bash
# ============================================================================
# Vels Claude - Update Script
# ============================================================================
#
# Обновление одной командой:
#
#   curl -sSL https://agent.nickvels.ru/releases/light-update.sh | sudo bash
#
# Или с нестандартной директорией:
#
#   curl -sSL https://agent.nickvels.ru/releases/light-update.sh | sudo INSTALL_DIR=/srv/my-bot bash
#
# Прямой запуск из приватного репо (для разработчиков, требует свой GH_TOKEN):
#
#   GH_TOKEN=ghp_xxx
#   curl -sSL -H "Authorization: token $GH_TOKEN" \
#     https://raw.githubusercontent.com/nick-vels/Vels-Claude/main/scripts/update.sh \
#     | sudo bash
#
# Что делает:
#   1. Находит установку бота на сервере
#   2. git pull (обновляет код)
#   3. pip install (обновляет зависимости если изменились)
#   4. systemctl restart (перезапускает бота)
#
# Что НЕ трогает:
#   - .env (конфигурация; ЕДИНСТВЕННОЕ исключение — идемпотентно ДОписывает
#     CONNECTIONS_SECRET_KEY, если его нет, чтобы включить SP2 (хранение
#     per-user Anthropic-ключей); существующий ключ НИКОГДА не трогает/ротирует)
#   - data/ (SQLite база, сессии)
#   - ~/.claude/ (настройки Claude Code)
#
# Безопасен для повторного запуска (идемпотентный).
# ============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; }

# ---------------------------------------------------------------------------
# CONNECTIONS_SECRET_KEY backfill (SP2 — per-user Anthropic API keys)
# ---------------------------------------------------------------------------
# Fernet-совместимый ключ: url-safe base64 от 32 случайных байт. ТОТ ЖЕ метод,
# что и install.sh::gen_connections_key — без этого ключа src/apikeys/service.py
# возвращает None и хранение per-user ключей молча не работает (/api/apikey → 501).
gen_connections_key() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32 | tr '+/' '-_'
    else
        python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    fi
}

# Идемпотентно обеспечивает CONNECTIONS_SECRET_KEY в существующем .env. Три случая:
#   • строка есть и НЕПУСТА → НИКОГДА не трогаем (ротация осиротит ВСЕ уже
#     зашифрованные per-user ключи — расшифровать станет невозможно);
#   • строка есть, но ПУСТА (`CONNECTIONS_SECRET_KEY=` без значения, возможны
#     хвостовые пробелы) → заполняем значение НА МЕСТЕ, без дубликата;
#   • строки нет вовсе → дописываем в конец.
# Правку «на месте» делаем через sed→temp→`cat >`: перезапись содержимого через
# `>` сохраняет тот же inode → владелец и права файла (640 root:<group> из
# install.sh) не меняются; `sed -i` избегаем (на BSD sed он несовместим). `>>`
# для дописывания тоже сохраняет владельца/права (update.sh иначе .env не трогает).
# Флаг: backfill реально дописал/заполнил ключ (значит .env изменился и живой
# процесс держит старый env без ключа → нужен рестарт, ИНАЧЕ SP2 останется молча
# выключенным даже на ранних выходах update.sh ниже). Инициализируется здесь,
# выставляется внутри backfill_connections_key.
CONNKEY_CHANGED=0

backfill_connections_key() {
    local env_file="$INSTALL_DIR/.env"
    [ -f "$env_file" ] || return 0
    if grep -qE '^CONNECTIONS_SECRET_KEY=.*[^[:space:]]' "$env_file" 2>/dev/null; then
        return 0  # уже есть непустой ключ — не трогаем
    fi
    local key
    key="$(gen_connections_key)"
    if grep -qE '^CONNECTIONS_SECRET_KEY=[[:space:]]*$' "$env_file" 2>/dev/null; then
        # Строка ключа есть, но пустая — заполняем НА МЕСТЕ, без дубля.
        local tmp
        tmp="$(mktemp)"
        sed -E "s|^CONNECTIONS_SECRET_KEY=[[:space:]]*\$|CONNECTIONS_SECRET_KEY=${key}|" \
            "$env_file" > "$tmp"
        cat "$tmp" > "$env_file"
        rm -f "$tmp"
        CONNKEY_CHANGED=1
        ok "Заполнен пустой CONNECTIONS_SECRET_KEY в .env (включает SP2 — хранение per-user API-ключей)"
    else
        printf 'CONNECTIONS_SECRET_KEY=%s\n' "$key" >> "$env_file"
        CONNKEY_CHANGED=1
        ok "Добавлен CONNECTIONS_SECRET_KEY в .env (включает SP2 — хранение per-user API-ключей)"
    fi
    if [ "$CONNKEY_CHANGED" = 1 ]; then
        # L-11: дефолт с коммита b0d1bbe — мягкий старт (require_user_key=false),
        # НЕ строгий режим. Старый текст советовал вручную дописать `=false`,
        # хотя это уже поведение по умолчанию — вводило в заблуждение.
        warn "SP2 (per-user API-ключи) теперь активен. Мягкий старт по умолчанию —"
        warn "ключи не обязательны. Строгий режим (обязать пользователей задать свой"
        warn "ключ) — opt-in: добавьте CLAUDE_REQUIRE_USER_KEY=true в .env."
    fi
}

# Best-effort рестарт сервиса. Нужен на РАННИХ выходах update.sh (release-install
# без .git; «уже актуально»), когда backfill только что добавил ключ: без рестарта
# живой процесс держал бы старый env без CONNECTIONS_SECRET_KEY и SP2 (/api/apikey)
# молчал бы до следующего несвязанного деплоя. No-op без systemctl (юнит-тесты).
restart_bot_service() {
    command -v systemctl >/dev/null 2>&1 || return 0
    local svc="${SERVICE_NAME:-vels-claude}"
    if ! systemctl is-active --quiet "$svc" 2>/dev/null \
        && systemctl is-active --quiet "telegram-claude-code" 2>/dev/null; then
        svc="telegram-claude-code"
    fi
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        info "Перезапускаю сервис — подхватить новый CONNECTIONS_SECRET_KEY..."
        systemctl restart "$svc" || true
    elif systemctl --user is-active --quiet "$svc" 2>/dev/null; then
        systemctl --user restart "$svc" || true
    fi
}

# ---------------------------------------------------------------------------
# M-6: MCP project-scope trust seed (~/.claude/settings.json)
# ---------------------------------------------------------------------------
# Свежий сервис-юзер (или установка ДО этого фикса) может не иметь
# ~/.claude/settings.json — headless-Claude без TTY требует ручного одобрения
# project-scope MCP-серверов из .mcp.json. install.sh уже сидит этот флаг для
# новых установок; здесь — идемпотентный шаг для уже обновляющихся ботов.
# Определяем сервис-юзера/HOME из уже установленного systemd-юнита (НЕ трогаем
# выбор сервис-юзера — это забота install.sh/M-2).
seed_claude_project_mcp_trust() {
    command -v systemctl >/dev/null 2>&1 || return 0
    command -v python3 >/dev/null 2>&1 || {
        warn "python3 не найден — пропускаю MCP-сид (~/.claude/settings.json)."
        return 0
    }
    local svc="${SERVICE_NAME:-vels-claude}"
    local unit_user
    # set -euo pipefail активен в этом скрипте — `|| true` защищает от
    # неожиданного нуля properties на отсутствующем юните (аналогично
    # существующему `BEFORE=$(git rev-parse HEAD 2>/dev/null || echo "unknown")` выше).
    unit_user="$(systemctl show -p User --value "$svc" 2>/dev/null || true)"
    if [ -z "$unit_user" ]; then
        svc="telegram-claude-code"
        unit_user="$(systemctl show -p User --value "$svc" 2>/dev/null || true)"
    fi
    [ -n "$unit_user" ] || return 0

    local unit_home
    unit_home="$(systemctl show -p Environment --value "$svc" 2>/dev/null \
        | tr ' ' '\n' | awk -F= '$1=="HOME"{print $2; exit}' || true)"
    if [ -z "$unit_home" ]; then
        unit_home="$(getent passwd "$unit_user" 2>/dev/null | cut -d: -f6 || true)"
    fi
    [ -n "$unit_home" ] || return 0

    local settings_dir="$unit_home/.claude"
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
    chown "$unit_user" "$settings_dir" "$settings_file" 2>/dev/null || true
    chmod 0600 "$settings_file" 2>/dev/null || true
    ok "MCP project-scope доверие сидировано: $settings_file"
}

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    echo "Vels Claude - Update Script"
    echo ""
    echo "Usage:"
    echo "  curl -sSL <url>/scripts/update.sh | sudo bash"
    echo "  sudo bash scripts/update.sh"
    echo ""
    echo "Environment variables:"
    echo "  INSTALL_DIR    Installation directory (default: auto-detect)"
    echo ""
    exit 0
fi

echo ""
echo -e "${BOLD}Vels Claude - Обновление${NC}"
echo "════════════════════════════════════════"
echo ""

# ---------------------------------------------------------------------------
# Step 0: Find installation
# ---------------------------------------------------------------------------

INSTALL_DIR="${INSTALL_DIR:-}"

if [ -n "$INSTALL_DIR" ]; then
    info "Используется INSTALL_DIR=$INSTALL_DIR"
else
    # Auto-detect: check common locations
    CANDIDATES=(
        "/opt/vels-claude"
        "/srv/vels-claude"
        "/opt/telegram-claude-code"
        "/srv/telegram-claude-code"
        "$HOME/vels-claude"
        "$HOME/telegram-claude-code"
    )

    # Also check if running from within the project
    if [ -f "./src/main.py" ] && [ -f "./requirements.txt" ]; then
        INSTALL_DIR="$(pwd)"
    else
        # Check BASH_SOURCE for local execution (not curl pipe)
        if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" != "bash" ]; then
            SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
            if [ -f "$SCRIPT_DIR/../src/main.py" ]; then
                INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
            fi
        fi
    fi

    # Fallback: scan candidates
    if [ -z "$INSTALL_DIR" ]; then
        for dir in "${CANDIDATES[@]}"; do
            # Не требуем .git: release-установки (из публичного архива) его не
            # имеют. Маркер любой установки — src/main.py; release-случай (нет
            # .git) ниже корректно роутится на дружелюбную подсказку.
            if [ -f "$dir/src/main.py" ]; then
                INSTALL_DIR="$dir"
                break
            fi
        done
    fi

    if [ -z "$INSTALL_DIR" ]; then
        error "Установка бота не найдена."
        error ""
        error "Проверены директории:"
        for dir in "${CANDIDATES[@]}"; do
            error "  - $dir"
        done
        error ""
        error "Укажите путь вручную:"
        error "  curl ... | INSTALL_DIR=/path/to/bot sudo -E bash"
        exit 1
    fi
fi

if [ ! -f "$INSTALL_DIR/src/main.py" ]; then
    error "$INSTALL_DIR не содержит Vels Claude."
    exit 1
fi

cd "$INSTALL_DIR"
ok "Найдена установка: $INSTALL_DIR"

# Бэкфилл SP2-ключа делаем СРАЗУ (до любого раннего выхода ниже — release-архив
# без .git выходит через пару строк), чтобы старые установки без него получили
# рабочее хранение per-user API-ключей при первом же обновлении.
backfill_connections_key

# M-6: тот же принцип — идемпотентный сид MCP-доверия ДО любого раннего выхода,
# чтобы release-установки (без git) тоже получили его при каждом update.sh.
seed_claude_project_mcp_trust

# Релиз-установка (из публичного архива) не содержит git-репозитория — обновлять
# через `git pull` нечего. Обновление = повторный запуск установщика: он скачает
# свежий релиз-архив и аккуратно положит код поверх (ваши .env и data/ целы).
if [ ! -d "$INSTALL_DIR/.git" ]; then
    warn "Это установка из релиз-архива (без git)."
    echo ""
    echo "  Чтобы обновиться — повторите команду установки:"
    echo "    curl -sSL https://agent.nickvels.ru/releases/light-install.sh | sudo bash"
    echo ""
    echo "  Ваши .env и data/ при этом не пострадают."
    # Если backfill только что добавил ключ — рестартим, чтобы SP2 заработал сразу.
    [ "$CONNKEY_CHANGED" = 1 ] && restart_bot_service
    exit 0
fi

# Каталог принадлежит сервисному юзеру, а скрипт идёт от root → git ≥ 2.35.2
# ругается «detected dubious ownership» и падает. Объявляем каталог доверенным,
# иначе git fetch/pull/stash ниже обрывают обновление.
git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 1: Save current version
# ---------------------------------------------------------------------------

BEFORE=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")

info "Текущая версия: $(echo "$BEFORE" | cut -c1-7) ($CURRENT_BRANCH)"

# ---------------------------------------------------------------------------
# Step 2: Stash local changes if any
# ---------------------------------------------------------------------------

if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    warn "Локальные изменения сохранены в git stash"
    git stash push -m "auto-stash before update $(date +%F_%T)" --quiet
fi

# ---------------------------------------------------------------------------
# Step 3: Pull latest code
# ---------------------------------------------------------------------------

info "Загружаю обновления..."

git fetch origin "$CURRENT_BRANCH" --quiet 2>/dev/null

REMOTE_HEAD=$(git rev-parse "origin/$CURRENT_BRANCH" 2>/dev/null || echo "")

if [ "$BEFORE" = "$REMOTE_HEAD" ]; then
    # Прод-копию держим чистой даже без новых коммитов: прун идемпотентен и
    # срезает тест/мусор, который авто-stash (Step 2) мог вернуть. См. Step 3.5.
    _proot="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
    rm -rf "$_proot/tests" "$_proot/web/src/__tests__" "$_proot/web/.vite" \
           "$_proot/web/dist_test" "$_proot/.pytest_cache" 2>/dev/null || true
    ok "Уже актуальная версия — обновление не требуется"
    # Если backfill только что добавил ключ — рестартим, чтобы SP2 заработал сразу
    # (иначе живой процесс держит старый env без ключа до следующего деплоя).
    [ "$CONNKEY_CHANGED" = 1 ] && restart_bot_service
    echo ""
    exit 0
fi

git pull origin "$CURRENT_BRANCH" --ff-only --quiet 2>/dev/null || {
    warn "Fast-forward невозможен, делаю merge..."
    git pull origin "$CURRENT_BRANCH" --no-edit --quiet
}

AFTER=$(git rev-parse HEAD)
COMMITS=$(git log --oneline "$BEFORE".."$AFTER" 2>/dev/null | wc -l | tr -d ' ')
ok "Обновлено: $COMMITS коммитов ($(echo "$BEFORE" | cut -c1-7) → $(echo "$AFTER" | cut -c1-7))"

# Show what changed.
# ВАЖНО: сначала собираем лог в переменную, и только потом режем `head`.
# Прямой `git log | head` под `set -o pipefail` + `set -e` ронял весь скрипт
# с кодом 141 (SIGPIPE: head закрывает пайп, git log получает SIGPIPE) —
# из-за этого обновление обрывалось ДО пересборки фронта и рестарта.
CHANGELOG=$(git log --oneline --no-decorate "$BEFORE".."$AFTER" 2>/dev/null || true)
echo ""
info "Изменения:"
printf '%s\n' "$CHANGELOG" | head -10 | sed 's/^/    /'
TOTAL=$(printf '%s\n' "$CHANGELOG" | grep -c . || true)
if [ "$TOTAL" -gt 10 ]; then
    echo "    ... и ещё $((TOTAL - 10)) коммитов"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 3.5: Прун — убрать с прод-копии то, что не нужно в деплое
# ---------------------------------------------------------------------------
# git pull воспроизводит ВЕСЬ tracked-дерево (.gitignore и .gitattributes
# export-ignore на pull НЕ влияют). Прод не нуждается в тестах и дев-мусоре —
# срезаем их после обновления. CLAUDE.md/README/пользовательские доки остаются.
# Срез делает дерево «грязным» → следующий запуск авто-стэшит и повторяет
# (self-healing, см. Step 2). Делаем ДО сборки фронта — тесты ей не нужны.
PRUNE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
rm -rf "$PRUNE_ROOT/tests" \
       "$PRUNE_ROOT/web/src/__tests__" \
       "$PRUNE_ROOT/web/.vite" \
       "$PRUNE_ROOT/web/dist_test" \
       "$PRUNE_ROOT/.pytest_cache" 2>/dev/null || true
info "Срезаны тестовые/мусорные файлы (прод-копия не нуждается в них)"

# ---------------------------------------------------------------------------
# Step 3.6: пересоздать legacy venv (H-2)
# ---------------------------------------------------------------------------
# Раньше update.sh НИКОГДА не трогал существующий .venv → бот, установленный
# на Ubuntu 22.04 (system python3=3.10), навсегда оставался на venv <3.11, даже
# когда requirements.lock требует rpds-py, собранный под 3.11. Проверяем
# версию ПЕРЕД любым pip-вызовом и пересоздаём venv, если она <3.11 (или venv
# сломан/отсутствует). pick_python/ensure_python311 — общий helper с install.sh.
VENV_RECREATED=0

recreate_legacy_venv() {
    local venv_dir="$INSTALL_DIR/.venv"
    if [ -x "$venv_dir/bin/python" ] \
        && "$venv_dir/bin/python" -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' >/dev/null 2>&1; then
        return 0  # уже >=3.11 — нечего чинить
    fi

    if ! command -v ensure_python311 >/dev/null 2>&1; then
        if [ -f "$INSTALL_DIR/scripts/lib/python-env.sh" ]; then
            # shellcheck source=/dev/null
            . "$INSTALL_DIR/scripts/lib/python-env.sh"
        fi
    fi
    if ! command -v ensure_python311 >/dev/null 2>&1; then
        warn "scripts/lib/python-env.sh не найден — пропускаю проверку версии venv."
        return 0
    fi

    local pybin
    if ! pybin="$(ensure_python311)"; then
        warn "Python >=3.11 не найден — venv не пересоздан (requirements.lock может не встать)."
        return 0
    fi

    warn "venv на Python <3.11 (или повреждён/отсутствует) — пересоздаю из $("$pybin" --version 2>&1)..."
    # Сохраняем владельца/права как у существующего каталога (H-7 у install.sh
    # держит .venv root-owned+read-only; более старые установки могли отдать
    # его сервис-юзеру) — пересоздание НЕ должно менять эту модель прав.
    local owner group
    if [ -e "$venv_dir" ]; then
        owner="$(stat -c '%U' "$venv_dir" 2>/dev/null || echo root)"
        group="$(stat -c '%G' "$venv_dir" 2>/dev/null || echo root)"
    else
        owner="$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo root)"
        group="$(stat -c '%G' "$INSTALL_DIR" 2>/dev/null || echo root)"
    fi
    rm -rf "$venv_dir"
    "$pybin" -m venv "$venv_dir"
    chown -R "$owner":"$group" "$venv_dir" 2>/dev/null || true
    VENV_RECREATED=1
    ok "venv пересоздан: $("$pybin" --version 2>&1)"
}

recreate_legacy_venv

# ---------------------------------------------------------------------------
# Step 4: Update dependencies (only if requirements.txt changed)
# ---------------------------------------------------------------------------

# H-9: реагируем и на requirements.lock (источник истины с хэшами), и на
# requirements.txt (на случай ручной правки). Ставим из lock с --require-hashes:
# pip отвергнет любой пакет, чей sha256 не совпал (защита supply-chain).
# Список изменённых файлов — один раз и без пайпа в условии (см. ниже).
CHANGED_FILES="$(git diff --name-only "$BEFORE".."$AFTER" 2>/dev/null || true)"

# VENV_RECREATED=1 (H-2) форсирует полную установку — свежий venv пуст, даже
# если requirements.lock/txt не менялись в этом диапазоне коммитов.
# CHANGED_FILES собран один раз выше: пайп в grep -q под pipefail
# ложноотрицателен — grep закрывает поток на первом совпадении, git ловит
# SIGPIPE, и «зависимости изменились» превращается в «не изменились».
if grep -qE "requirements\.(lock|txt)" <<<"$CHANGED_FILES" \
   || [ "$VENV_RECREATED" = 1 ]; then
    info "Зависимости изменились — обновляю..."
    PIP_BIN=""
    if [ -d ".venv" ]; then
        PIP_BIN=".venv/bin/pip"
    elif [ -d "venv" ]; then
        PIP_BIN="venv/bin/pip"
    else
        PIP_BIN="pip3"
    fi
    if [ -f "requirements.lock" ]; then
        "$PIP_BIN" install -q --require-hashes --no-deps -r requirements.lock
    else
        warn "requirements.lock отсутствует — ставлю из requirements.txt БЕЗ проверки хэшей."
        "$PIP_BIN" install -q -r requirements.txt
    fi
    ok "Зависимости обновлены"
else
    ok "Зависимости не изменились — пропускаю"
fi

# ---------------------------------------------------------------------------
# Step 4.5: Rebuild web frontend (web UI served from web/dist)
# ---------------------------------------------------------------------------

if [ -f "$INSTALL_DIR/web/package.json" ]; then
    if grep -q "^web/" <<<"$CHANGED_FILES" \
       || [ ! -f "$INSTALL_DIR/web/dist/index.html" ]; then
        if command -v npm >/dev/null 2>&1; then
            info "Пересобираю веб-интерфейс..."
            OWNER="$(stat -c '%U' "$INSTALL_DIR" 2>/dev/null || echo root)"
            # VITE_BASE под режим: /agent/ ТОЛЬКО когда public_origin содержит
            # путь /agent ПОСЛЕ хоста (IP-режим, http://<ip>/agent). Домен вида
            # https://agent.nickvels.ru НЕ должен срабатывать — в "https://agent"
            # подстрока "/agent" это часть "://", а не путь (старый регэксп
            # 'public_origin:.*/agent' ложно матчил такой домен и ломал прод,
            # собирая ассеты на /agent/assets вместо /assets).
            VITE_BASE="/"
            if grep -qE '^[[:space:]]*public_origin:[[:space:]]*"?[a-zA-Z]+://[^/"]+/agent' "$INSTALL_DIR/config/config.local.yaml" 2>/dev/null; then
                VITE_BASE="/agent/"
            fi
            # Имя бота в сборку не идёт: страница входа спрашивает его у
            # сервера (/api/auth/config), поэтому пересборка не может «потерять»
            # виджет, а веб-only установка не обещает несуществующего бота.
            BUILD_CMD="cd '$INSTALL_DIR/web' && npm ci && VITE_BASE='$VITE_BASE' npm run build"
            if [ "$OWNER" != "root" ] && id "$OWNER" >/dev/null 2>&1; then
                if sudo -u "$OWNER" -H bash -lc "$BUILD_CMD"; then
                    ok "Веб-интерфейс пересобран"
                else
                    warn "Сборка фронта не удалась — проверьте Node/npm вручную"
                fi
            else
                if bash -lc "$BUILD_CMD"; then
                    ok "Веб-интерфейс пересобран"
                else
                    warn "Сборка фронта не удалась"
                fi
            fi
        else
            warn "npm не найден — пропускаю пересборку фронта (web UI может остаться старым)"
        fi
    else
        ok "Веб-интерфейс не изменился — пересборка не требуется"
    fi
    # Веб-код есть, но сервер не настроен на веб (старая Telegram-only установка):
    # update НЕ трогает .env/config — подсказываем включить через install.sh.
    if ! grep -q '^WEB_JWT_SECRET=' "$INSTALL_DIR/.env" 2>/dev/null \
       || [ ! -f "$INSTALL_DIR/config/config.local.yaml" ]; then
        warn "Веб-интерфейс в коде есть, но на этом сервере не настроен (нет web-конфига)."
        warn "Чтобы включить веб-UI (и SP2 — хранение per-user Anthropic-ключей) — запустите установку повторно:"
        warn "  curl -sSL https://agent.nickvels.ru/releases/light-install.sh | sudo bash"
    fi
fi

# ---------------------------------------------------------------------------
# Step 5: Restart service
# ---------------------------------------------------------------------------

SERVICE_NAME="${SERVICE_NAME:-vels-claude}"

ACTIVE_SERVICE="$SERVICE_NAME"
if ! systemctl is-active --quiet "$ACTIVE_SERVICE" 2>/dev/null \
    && systemctl is-active --quiet "telegram-claude-code" 2>/dev/null; then
    ACTIVE_SERVICE="telegram-claude-code"
fi

if systemctl is-active --quiet "$ACTIVE_SERVICE" 2>/dev/null; then
    info "Перезапускаю сервис..."
    systemctl restart "$ACTIVE_SERVICE"
    sleep 2
    if systemctl is-active --quiet "$ACTIVE_SERVICE"; then
        ok "Сервис перезапущен и работает"
    else
        error "Сервис не запустился!"
        echo ""
        echo "  Логи: journalctl -u $ACTIVE_SERVICE -n 30 --no-pager"
        exit 1
    fi
elif systemctl --user is-active --quiet "$ACTIVE_SERVICE" 2>/dev/null; then
    systemctl --user restart "$ACTIVE_SERVICE"
    sleep 2
    ok "User-level сервис перезапущен"
else
    warn "systemd-сервис не найден — перезапустите вручную"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo -e "${GREEN}  Обновление завершено!${NC}"
echo -e "${GREEN}════════════════════════════════════════${NC}"
echo ""
echo "  Версия: $(echo "$AFTER" | cut -c1-7)"
echo ""
echo "  Полезные команды:"
echo "    journalctl -u $ACTIVE_SERVICE -f        # логи"
echo "    systemctl status $ACTIVE_SERVICE         # статус"
echo ""
