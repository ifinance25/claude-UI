#!/usr/bin/env bash
# Backward-compatible wrapper. Production docs use scripts/install.sh.
set -euo pipefail

if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    exec "$SCRIPT_DIR/install.sh" "$@"
fi

curl -sSL https://raw.githubusercontent.com/nick-vels/Vels-Claude/main/scripts/install.sh | bash "$@"
