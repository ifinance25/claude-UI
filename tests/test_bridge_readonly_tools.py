from src.claude.bridge import READONLY_DISALLOWED_TOOLS, ClaudeBridge


def test_readonly_disallows_mutating_tools() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions")
    args = bridge._build_cli_args(message="hi", session_id=None, readonly=True)
    assert "--disallowedTools" in args
    idx = args.index("--disallowedTools")
    value = args[idx + 1]
    for tool in ("Write", "Edit", "Bash"):
        assert tool in value
    # В readonly не должно быть обхода разрешений.
    assert "--dangerously-skip-permissions" not in args


def test_full_access_has_no_disallow() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions")
    args = bridge._build_cli_args(message="hi", session_id=None, readonly=False)
    assert "--disallowedTools" not in args
    assert "--dangerously-skip-permissions" in args


def test_readonly_constant_covers_writes() -> None:
    assert {"Write", "Edit", "Bash"} <= set(READONLY_DISALLOWED_TOOLS)
