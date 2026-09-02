#!/usr/bin/env bash
# Integration test: conditional owner-credential bind in vels-claude-jail.sh.
#
# Verifies that VELS_JAIL_NO_OWNER_CREDS gates whether the owner's
# ~/.claude/.credentials.json is mounted into the bubblewrap jail:
#   - flag=1 (BYO-key)  -> creds ABSENT inside jail (empty tmpfs only)
#   - unset/other       -> creds PRESENT inside jail (default behaviour)
#
# The static shell-syntax check runs everywhere. The runtime bind checks need
# Linux + bubblewrap (`bwrap`); on hosts without it (e.g. the Windows/macOS dev
# box) the test SKIPs cleanly instead of failing.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
JAIL="$REPO_ROOT/scripts/vels-claude-jail.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
skip() { echo "SKIP: $*"; exit 0; }

[ -f "$JAIL" ] || fail "jail script not found: $JAIL"

# --- 1) Static syntax check (always) ----------------------------------------
sh -n "$JAIL" || fail "shell syntax error in $JAIL"
echo "OK: shell syntax valid"

# --- 2) Runtime bind checks (Linux + bubblewrap only) -----------------------
command -v bwrap >/dev/null 2>&1 \
  || skip "bwrap not installed; runtime bind checks require Linux + bubblewrap"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Fake owner HOME holding the owner's credentials + settings.
FAKE_HOME="$WORK/home"
mkdir -p "$FAKE_HOME/.claude"
printf '{"claudeAiOauth":{"accessToken":"OWNER-TOKEN"}}' \
  > "$FAKE_HOME/.claude/.credentials.json"
printf '{"model":"opus"}' > "$FAKE_HOME/.claude/settings.json"

# Fake project root — must exist and be bind-mounted; also hosts the fake
# "claude" binary so the interpreter path is visible inside the jail.
PROJECT="$WORK/project"
mkdir -p "$PROJECT"

# Fake "claude": reports whether the owner creds are visible inside the jail,
# then exits cleanly (simulates a clean Claude run).
FAKE_CLAUDE="$PROJECT/fake-claude.sh"
cat > "$FAKE_CLAUDE" <<'EOF'
#!/bin/sh
if [ -f "$HOME/.claude/.credentials.json" ]; then
  echo "CREDS:PRESENT"
else
  echo "CREDS:ABSENT"
fi
exit 0
EOF
chmod +x "$FAKE_CLAUDE"

# Run the jail with a given VELS_JAIL_NO_OWNER_CREDS value ("" => leave unset).
run_jail() {
  flagval="$1"
  if [ -n "$flagval" ]; then
    VELS_JAIL_NO_OWNER_CREDS="$flagval" \
    VELS_PROJECT_ROOT="$PROJECT" VELS_REAL_CLAUDE="$FAKE_CLAUDE" \
    VELS_CLAUDE_CREDS_DIR="$FAKE_HOME/.claude" HOME="$FAKE_HOME" \
      sh "$JAIL"
  else
    VELS_PROJECT_ROOT="$PROJECT" VELS_REAL_CLAUDE="$FAKE_CLAUDE" \
    VELS_CLAUDE_CREDS_DIR="$FAKE_HOME/.claude" HOME="$FAKE_HOME" \
      sh "$JAIL"
  fi
}

# --- Test 1: BYO-key mode -> owner creds NOT mounted ------------------------
out1="$(run_jail 1)"; rc1=$?
[ "$rc1" -eq 0 ] || fail "Test1: jail exited $rc1 (expected clean 0). Output: $out1"
printf '%s\n' "$out1" | grep -q "CREDS:ABSENT" \
  || fail "Test1: expected owner creds ABSENT with VELS_JAIL_NO_OWNER_CREDS=1, got: $out1"
echo "OK Test1: BYO-key mode — owner .credentials.json NOT accessible in jail"

# --- Test 2: default mode -> owner creds mounted (existing behaviour) -------
out2="$(run_jail "")"; rc2=$?
[ "$rc2" -eq 0 ] || fail "Test2: jail exited $rc2 (expected clean 0). Output: $out2"
printf '%s\n' "$out2" | grep -q "CREDS:PRESENT" \
  || fail "Test2: expected owner creds PRESENT without the flag, got: $out2"
echo "OK Test2: default mode — owner .credentials.json accessible in jail"

echo "PASS: conditional owner-credential bind works in both modes"
