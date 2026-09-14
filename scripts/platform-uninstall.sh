#!/usr/bin/env bash
# Public bootstrap for AI-Panel uninstaller.
# Залить на сервер как: <platform-repo>/public/uninstall.sh
# Доступен по адресу:   https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/uninstall.sh
#
# Аналог platform-install.sh, но для удаления установленного бота.
# Использует тот же зашитый PAT для скачивания scripts/uninstall.sh
# из приватного репо.
#
# Ротация токена — см. инструкцию в platform-install.sh.

set -euo pipefail

GH_TOKEN="REPLACE_WITH_FINE_GRAINED_PAT"

REPO_OWNER="${REPO_OWNER:-ifinance25}"
REPO_NAME="${REPO_NAME:-claude-UI}"
REPO_BRANCH="${REPO_BRANCH:-main}"

if [[ "$GH_TOKEN" == "REPLACE_WITH_FINE_GRAINED_PAT" || -z "$GH_TOKEN" ]]; then
    echo "[ERROR] Bootstrap не настроен: отсутствует GH_TOKEN." >&2
    echo "        Проверьте GH_TOKEN в scripts/platform-uninstall.sh." >&2
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "[ERROR] Требуются root-права. Запустите через sudo:" >&2
    echo "        curl -sSL https://raw.githubusercontent.com/ifinance25/claude-UI/main/scripts/uninstall.sh | sudo bash" >&2
    exit 1
fi

export GH_TOKEN

curl -fsSL \
    -H "Authorization: Bearer ${GH_TOKEN}" \
    -H "Accept: application/vnd.github.v3.raw" \
    "https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/contents/scripts/uninstall.sh?ref=${REPO_BRANCH}" \
    | bash
