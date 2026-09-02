#!/usr/bin/env bash
# scripts/lib/python-env.sh — общий sourceable helper выбора/установки Python >=3.11.
#
# Используется И install.sh, И update.sh: проект ТРЕБУЕТ Python >=3.11
# (pyproject requires-python, а rpds-py в requirements.lock собран под 3.11) —
# на Ubuntu 22.04 системный python3=3.10, поэтому venv из него ломает pip
# ("No matching distribution found for rpds-py"). H-2: раньше эта логика жила
# только в install.sh — update.sh никогда не пересоздавал venv, и фикс не
# доезжал до уже установленных ботов. Теперь оба скрипта используют один и тот
# же код.
#
# Контракт для sourcing-скрипта:
#   - если он уже определяет log_warn()/die() (как install.sh) — используются они;
#   - иначе (update.sh) — ниже подставляются минимальные заглушки в stderr.

if ! declare -F log_warn >/dev/null 2>&1; then
    log_warn() { printf '[WARN] %s\n' "$*" >&2; }
fi
if ! declare -F die >/dev/null 2>&1; then
    die() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
fi

pick_python() {
    # Печатает путь к интерпретатору Python >=3.11. Перебираем явные версии
    # 3.13→3.12→3.11, затем plain python3, но берём его ТОЛЬКО если он сам >=3.11.
    # Возвращает !=0, если подходящего интерпретатора нет.
    #
    # releaselevel == 'final' — обязательное условие, а не придирка: в universe
    # Ubuntu 22.04 под именем python3.11 лежит 3.11.0~rc1 (release candidate от
    # октября 2022). Он проходит проверку version_info >= (3, 11), после чего
    # ensure_python311 считает задачу решённой и до deadsnakes с настоящим
    # релизом дело не доходит. RC не получает патчей безопасности — в проде у
    # тысячи человек ему не место. Поймано прогоном на живом Ubuntu 22.04.
    local cand
    for cand in python3.13 python3.12 python3.11 python3; do
        command -v "$cand" >/dev/null 2>&1 || continue
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) and sys.version_info.releaselevel == "final" else 1)' >/dev/null 2>&1; then
            command -v "$cand"
            return 0
        fi
    done
    return 1
}

ensure_python311() {
    # Гарантирует наличие Python >=3.11 и печатает путь к нему (на stdout —
    # чтобы вызвать как PYBIN="$(ensure_python311)"). Если готового нет — пытается
    # доставить его через apt (python3.12 → python3.11), а на старых Ubuntu (22.04,
    # где их нет в базовых репах) — через PPA deadsnakes. Каждый apt-шаг защищён
    # (|| true): транзиентная ошибка apt НЕ должна ронять установщик до попытки
    # фоллбэка. Диагностика/вывод apt уводятся в stderr, чтобы не засорять stdout.
    local pybin
    if pybin="$(pick_python)"; then
        printf '%s\n' "$pybin"
        return 0
    fi

    log_warn "Python >=3.11 не найден — пытаюсь установить (проект требует >=3.11)." >&2
    # 1) Штатные репы дистрибутива: сначала 3.12, затем 3.11 (что есть в наличии).
    apt-get update -qq >&2 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12 python3.12-venv >&2 || true
    if ! pick_python >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv >&2 || true
    fi
    # 2) Старые Ubuntu (22.04): 3.11/3.12 отсутствуют в базовых репах → PPA deadsnakes.
    if ! pick_python >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq software-properties-common >&2 || true
        add-apt-repository -y ppa:deadsnakes/ppa >&2 || true
        apt-get update -qq >&2 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv >&2 || true
    fi

    if pybin="$(pick_python)"; then
        printf '%s\n' "$pybin"
        return 0
    fi
    die "Установите Python ≥3.11 (sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.11 python3.11-venv) и запустите установку снова."
}
