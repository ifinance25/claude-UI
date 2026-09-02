#!/bin/sh
# vels-claude-jail.sh — used as ClaudeAgentOptions.cli_path for CONFINED sessions.
# Runs the real `claude` inside a bubblewrap filesystem jail. Only the session's
# project dir (+ minimal read-only system + creds) is visible; /home, /opt, /root,
# other projects are ABSENT. Fails closed: if bwrap is missing, exec fails and no
# claude runs. Params come from env, set by the bridge for confined sessions.
#
# Owner credentials are bound read-only by DEFAULT: .credentials.json (with
# --ro-bind-try, so an env-key-only owner without that file does not break the
# jail — M-9) and a SANITIZED settings.json (VELS_JAIL_SETTINGS_FILE, provided
# by the bridge with MCP-approval keys stripped — M-7). In BYO-key mode
# (VELS_JAIL_NO_OWNER_CREDS=1, set by the bridge when a per-user
# ANTHROPIC_API_KEY is injected) neither is bound: ~/.claude stays an empty
# tmpfs and the user's own key is the only credential source.
set -eu
: "${VELS_PROJECT_ROOT:?jail: VELS_PROJECT_ROOT unset}"
: "${VELS_REAL_CLAUDE:?jail: VELS_REAL_CLAUDE unset}"
: "${HOME:?jail: HOME unset}"
CREDS_DIR="${VELS_CLAUDE_CREDS_DIR:-$HOME/.claude}"

# Assemble bwrap's argv by PREPENDING onto "$@" (which currently holds the real
# claude CLI args). Each `set -- NEW "$@"` inserts NEW *before* the existing
# args, so the blocks below are written in REVERSE of their final runtime order
# (innermost/last-arg first). Final argv:
#   bwrap <mounts> [<owner-creds>] <namespaces> "$VELS_REAL_CLAUDE" <claude args>

# 4) the real claude binary, immediately before its own args.
set -- "$VELS_REAL_CLAUDE" "$@"

# 3) namespaces / env / working dir.
set -- \
  --setenv HOME "$HOME" --chdir "$VELS_PROJECT_ROOT" \
  --unshare-pid --unshare-ipc --unshare-uts --die-with-parent --new-session \
  "$@"

# 2) owner credentials — ONLY outside BYO-key mode. In BYO-key mode
#    (VELS_JAIL_NO_OWNER_CREDS=1) these are skipped, so ~/.claude stays an empty
#    tmpfs and the owner's .credentials.json is ABSENT inside the jail.
if [ "${VELS_JAIL_NO_OWNER_CREDS:-}" != "1" ]; then
  # M-9: .credentials.json bound with --ro-bind-TRY. When the owner authorises
  # via an env ANTHROPIC_API_KEY instead of `claude /login`, this file does not
  # exist — a hard --ro-bind failed ("Can't find source path") on the FIRST
  # message of every confined user. -try skips it; the env key (kept in the
  # child env by _clean_env when no ~/.claude creds exist) is then the auth
  # source inside the jail.
  set -- \
    --ro-bind-try "$CREDS_DIR/.credentials.json" "$HOME/.claude/.credentials.json" \
    "$@"
  # M-7: mount the bridge-SANITIZED settings.json (VELS_JAIL_SETTINGS_FILE), NOT
  # the raw owner file — the raw one may carry enableAllProjectMcpServers, which
  # would auto-start a project's .mcp.json servers at session init (before the
  # firewall) = RCE for confined sessions. If the bridge did not provide one,
  # mount nothing.
  if [ -n "${VELS_JAIL_SETTINGS_FILE:-}" ]; then
    set -- \
      --ro-bind-try "$VELS_JAIL_SETTINGS_FILE" "$HOME/.claude/settings.json" \
      "$@"
  fi
fi

# 1) base mounts (front of argv). ~/.claude tmpfs is always present.
set -- \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /sbin /sbin \
  --ro-bind /lib /lib --ro-bind-try /lib64 /lib64 \
  --ro-bind /etc/ssl /etc/ssl --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --ro-bind-try /etc/ca-certificates /etc/ca-certificates \
  --ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf \
  --proc /proc --dev /dev --tmpfs /tmp --tmpfs /run \
  --bind "$VELS_PROJECT_ROOT" "$VELS_PROJECT_ROOT" \
  --tmpfs "$HOME/.claude" \
  "$@"

exec bwrap "$@"
