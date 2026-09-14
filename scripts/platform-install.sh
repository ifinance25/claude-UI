#!/usr/bin/env bash
# Публичный бутстрап AI-Panel — раздаётся как
# https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh
#
# Имена архивов задаются EXPECTED_SHA256 / RELEASE_URL.
#
# Назначение: пользователь ставит Claude Code с веб-интерфейсом себе на сервер
# ОДНОЙ командой. Telegram-бот опционален (токен можно не вводить).
#
#   curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo bash
#
# ─────────────────────────────────────────────────────────────────────────────
# БЕЗ СЕКРЕТОВ. В этом файле НЕТ и НЕ ДОЛЖНО БЫТЬ GitHub-токена.
#
# Раньше здесь был зашит fine-grained PAT, чтобы клонировать ПРИВАТНЫЙ репозиторий.
# Любой, кто скачивал публичный install.sh, получал рабочий токен на чтение репо —
# это утечка по определению. Теперь установщик качает ПУБЛИЧНЫЙ релиз-архив
# (tar.gz, собранный из tracked-файлов через `git archive` — без .env/.git/data),
# проверяет его SHA-256 и распаковывает. Никакого токена → утечь нечему.
#
# Как обновить раздаваемый релиз (делает мейнтейнер):
#   1. scripts/make-release.sh origin/main        # печатает SHA256=
#      OUT_DIR=<webroot>/releases scripts/make-release.sh origin/main
#   2. Вписать напечатанный SHA256 в EXPECTED_SHA256 ниже (или передать env-ом).
#   3. Перезалить этот файл как <webroot>/install.sh.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Публичный URL релиз-архива (раздаётся nginx с того же хоста). Можно
# переопределить через окружение для тестовых стендов.
RELEASE_URL="${RELEASE_URL:-https://github.com/ifinance25/claude-UI/archive/refs/heads/main.tar.gz}"

# SHA-256 ожидаемого архива. Мейнтейнер вписывает вывод make-release.sh. Если
# оставить плейсхолдер — установка прервётся (fail-closed: не запускаем непроверенное).
EXPECTED_SHA256="${EXPECTED_SHA256:-REPLACE_WITH_TARBALL_SHA256}"

err() { echo "[ERROR] $*" >&2; }

if [[ $EUID -ne 0 ]]; then
    err "Требуются root-права. Запустите через sudo:"
    err "  curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/install.sh | sudo bash"
    exit 1
fi

command -v curl >/dev/null 2>&1 || { err "curl не найден."; exit 1; }
command -v tar  >/dev/null 2>&1 || { err "tar не найден.";  exit 1; }

SHA_TOOL=""
if command -v sha256sum >/dev/null 2>&1; then
    SHA_TOOL="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    SHA_TOOL="shasum -a 256"
else
    err "Нет sha256sum/shasum — не могу проверить контрольную сумму архива."
    exit 1
fi

if [[ ! "$EXPECTED_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
    err "Bootstrap не настроен: EXPECTED_SHA256 должен быть sha256 релиз-архива."
    err "Задайте EXPECTED_SHA256 (sha256 релиз-архива) или ставьте через scripts/install.sh."
    exit 1
fi

work="$(mktemp -d)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

# Скачиваем архив (обычный HTTPS, без токена). --proto/--proto-redir '=https'
# запрещают молчаливый downgrade на http по редиректу (defense-in-depth к sha).
if ! curl --proto '=https' --proto-redir '=https' -fsSL "$RELEASE_URL" -o "$work/release.tar.gz"; then
    err "Не удалось скачать релиз-архив: $RELEASE_URL"
    exit 1
fi

# Сверяем контрольную сумму ДО распаковки/запуска (fail-closed, главный контроль).
actual_sha="$($SHA_TOOL "$work/release.tar.gz" | awk '{print $1}')"
if [[ "$actual_sha" != "$EXPECTED_SHA256" ]]; then
    err "SHA-256 архива не совпал — установка ПРЕРВАНА (возможна подмена)."
    err "  ожидалось: $EXPECTED_SHA256"
    err "  получено:  $actual_sha"
    err "  Если вы только что опубликовали новый релиз — обновите EXPECTED_SHA256"
    err "  в install.sh значением из вывода make-release.sh."
    exit 1
fi

# Defense-in-depth перед распаковкой от root: отвергаем любой член вне префикса
# vels-claude/ и пути с '..' (traversal/symlink-атаки на чужие каталоги).
while IFS= read -r member; do
    case "$member" in
        vels-claude/*) ;;
        *) err "Архив содержит неожиданный путь: $member — релиз отклонён."; exit 1 ;;
    esac
    case "$member" in
        *..*) err "Архив содержит '..' в пути: $member — релиз отклонён."; exit 1 ;;
    esac
done < <(tar -tzf "$work/release.tar.gz")

# --no-same-owner/--no-same-permissions: член архива не навяжет чужого
# владельца/прав при распаковке от root.
tar --no-same-owner --no-same-permissions -xzf "$work/release.tar.gz" -C "$work"
src="$work/vels-claude"
if [[ -L "$src/scripts/install.sh" || ! -f "$src/scripts/install.sh" ]]; then
    err "scripts/install.sh отсутствует или не обычный файл — повреждён релиз."
    exit 1
fi
# Защита от архива, собранного из СТАРОГО install.sh (без release-режима):
# иначе install.sh проигнорировал бы RELEASE_SRC и ушёл бы в git clone приватного.
if ! grep -q 'RELEASE_SRC' "$src/scripts/install.sh"; then
    err "Релиз собран из старого install.sh без RELEASE_SRC — установка прервана."
    exit 1
fi

echo "[OK] Релиз-архив проверен (SHA-256 совпал). Запускаю установку..."

# install.sh в release-режиме копирует распакованный код в INSTALL_DIR без git
# и без токена, затем интерактивно спрашивает данные пользователя.
export RELEASE_SRC="$src"
exec bash "$src/scripts/install.sh" "$@"
