#!/usr/bin/env bash
# make-release.sh — собрать ПУБЛИЧНЫЙ релиз-архив Vels Claude для раздачи в
# комьюнити. Использует `git archive`, поэтому в архив попадают только
# отслеживаемые файлы (ни .env, ни data/, ни .venv, ни .git).
#
# ВАЖНО: tracked ≠ несекретный. git archive ИГНОРИРУЕТ .gitignore, поэтому
# внутренние доки (docs/**, CLAUDE.md, …) отсекаются через `.gitattributes`
# (export-ignore) + флаг --worktree-attributes ниже. Иначе HANDOFF-доки с
# прод-IP/ssh/deploy-key уехали бы в публичный архив. Регресс-тест
# tests/test_installer.py сканирует собранный архив на прод-маркеры.
#
# Использование:
#   scripts/make-release.sh [ref]        # ref по умолчанию = HEAD
#   OUT_DIR=/var/www/platform/releases scripts/make-release.sh origin/main
#
# Печатает TARBALL=/REF=/SHA256= — значение SHA256 нужно вписать в публичный
# бутстрап (platform-install.sh → EXPECTED_SHA256), чтобы установщик проверял
# целостность скачанного архива перед запуском.

set -euo pipefail

REF="${1:-HEAD}"
OUT_DIR="${OUT_DIR:-dist}"
PREFIX="vels-claude/"

command -v git >/dev/null 2>&1 || { echo "[ERROR] git не найден" >&2; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || { echo "[ERROR] make-release.sh нужно запускать внутри git-репозитория" >&2; exit 1; }

SHA_TOOL=""
if command -v sha256sum >/dev/null 2>&1; then
    SHA_TOOL="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    SHA_TOOL="shasum -a 256"
else
    echo "[ERROR] нет sha256sum/shasum — не могу посчитать контрольную сумму" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"

# Дружелюбная проверка ref (вместо сырого `fatal: ambiguous argument`).
if ! full_sha="$(git rev-parse --verify "${REF}^{commit}" 2>/dev/null)"; then
    echo "[ERROR] ref '$REF' не найден (опечатка ветки/тега?)." >&2
    exit 1
fi
short_sha="$(git rev-parse --short=12 "$full_sha")"

# Fail-closed на грязное дерево: git archive пакует ТОЛЬКО закоммиченный ref,
# поэтому незакоммиченные правки молча уехали бы устаревшими (напр. старый
# install.sh без RELEASE_SRC → установка у людей падает). Осознанная сборка из
# текущего состояния — ALLOW_DIRTY=1 (используется тестами).
if [[ "${ALLOW_DIRTY:-0}" != "1" ]]; then
    if ! git diff --quiet || ! git diff --cached --quiet \
       || [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
        echo "[ERROR] рабочее дерево грязное (незакоммиченные/неотслеживаемые изменения)." >&2
        echo "        git archive пакует только закоммиченный ref ($REF) — релиз вышел бы устаревшим." >&2
        echo "        Закоммитьте и запушьте, затем: scripts/make-release.sh origin/main" >&2
        echo "        (осознанно собрать из текущего HEAD: ALLOW_DIRTY=1 scripts/make-release.sh)" >&2
        exit 1
    fi
fi

tarball="$OUT_DIR/vels-claude-${short_sha}.tar.gz"

# --worktree-attributes: учитывать .gitattributes (export-ignore внутренних
# доков) ещё и из рабочего дерева — иначе при сборке до коммита .gitattributes
# секреты-доки уехали бы в архив. На чистом дереве (норма) рабочие и
# закоммиченные атрибуты совпадают.
git archive --worktree-attributes --format=tar.gz --prefix="$PREFIX" -o "$tarball" "$REF"

# Атомарная публикация «latest»: пишем во временный файл в ТОМ ЖЕ каталоге и
# переименовываем (rename в пределах ФС атомарен) — nginx никогда не отдаст
# наполовину записанный архив во время раздачи.
latest="$OUT_DIR/vels-claude-latest.tar.gz"
tmp_latest="$(mktemp "$OUT_DIR/.vels-claude-latest.XXXXXX")"
cp -f "$tarball" "$tmp_latest"
mv -f "$tmp_latest" "$latest"

# nginx (www-data) ДОЛЖЕН читать архив. mktemp создаёт файл с 0600 → после mv
# `latest` остался бы доступен только root, и веб-раздача падала бы с 403.
# Явно делаем оба архива world-readable (как ожидает статика nginx).
chmod 644 "$tarball" "$latest"

# SHA считаем по ИМЕННО тому файлу, который качает установщик (latest) — убирает
# неявную связку «версионный == latest». Сайдкар + готовая строка для вставки в
# platform-install.sh (EXPECTED_SHA256).
sha="$($SHA_TOOL "$latest" | awk '{print $1}')"
echo "$sha  vels-claude-latest.tar.gz" > "$latest.sha256"

echo "TARBALL=$tarball"
echo "REF=$full_sha"
echo "SHA256=$sha"
echo "EXPECTED_SHA256=$sha"

# L-12: опциональная авто-синхронизация EXPECTED_SHA256 в указанные бутстрап-
# файлы (platform-install.sh и его копии на разных vhost'ах). Регресс: 2 копии
# бутстрапа (agent/platform) разошлись по SHA, протухший EXPECTED_SHA256
# fail-closed заблокировал обновление через архив — пришлось руками
# синхронизировать все 3 копии на сервере. Дефолт (без SYNC_BOOTSTRAPS) —
# поведение НЕ меняется, только печать SHA. НЕ переходим на HTTPS-сайдкар —
# EXPECTED_SHA256 остаётся out-of-band anchor'ом, зашитым в сам бутстрап.
#
# Использование:
#   SYNC_BOOTSTRAPS="/var/www/vels-installer/install.sh /home/vlad/platform/app/public/install.sh" \
#     scripts/make-release.sh origin/main
if [[ -n "${SYNC_BOOTSTRAPS:-}" ]]; then
    # Список путей через пробел (как в контракте задачи) — намеренный
    # word-split, это maintainer-ops инструмент с доверенным вводом.
    # shellcheck disable=SC2086
    for bootstrap in $SYNC_BOOTSTRAPS; do
        if [[ ! -f "$bootstrap" ]]; then
            echo "[WARN] SYNC_BOOTSTRAPS: файл не найден, пропускаю: $bootstrap" >&2
            continue
        fi
        if ! grep -q '^EXPECTED_SHA256=' "$bootstrap"; then
            echo "[WARN] SYNC_BOOTSTRAPS: нет строки EXPECTED_SHA256= в файле, пропускаю: $bootstrap" >&2
            continue
        fi
        # Сохраняем исходные права (mktemp даёт 0600 — бутстрап должен остаться
        # world-readable для nginx, иначе публичная раздача упадёт с 403).
        orig_mode=""
        if stat -c '%a' "$bootstrap" >/dev/null 2>&1; then
            orig_mode="$(stat -c '%a' "$bootstrap")"
        else
            orig_mode="$(stat -f '%Lp' "$bootstrap" 2>/dev/null || true)"
        fi
        [[ -n "$orig_mode" ]] || orig_mode=644
        # Атомарно: temp-файл В ТОМ ЖЕ каталоге + rename — бутстрап никогда не
        # отдаётся наполовину переписанным во время раздачи.
        tmp_bootstrap="$(mktemp "$(dirname "$bootstrap")/.$(basename "$bootstrap").XXXXXX")"
        sed -E "s|^EXPECTED_SHA256=.*|EXPECTED_SHA256=\"${sha}\"|" "$bootstrap" >"$tmp_bootstrap"
        chmod "$orig_mode" "$tmp_bootstrap"
        mv -f "$tmp_bootstrap" "$bootstrap"
        echo "[OK] EXPECTED_SHA256 обновлён: $bootstrap"
    done
fi
