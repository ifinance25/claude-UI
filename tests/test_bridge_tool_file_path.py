from src.claude.bridge import ClaudeBridge, ClaudeEventType


def test_write_tool_use_carries_file_path() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions")
    raw = {"name": "Write", "input": {"file_path": "docs/report.md", "content": "x"}}
    ev = bridge._tool_use_event(raw)
    assert ev.type == ClaudeEventType.TOOL_USE
    assert ev.metadata.get("name") == "Write"
    assert ev.metadata.get("file_path") == "docs/report.md"


def test_read_tool_use_has_no_file_artifact() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions")
    raw = {"name": "Read", "input": {"file_path": "docs/report.md"}}
    ev = bridge._tool_use_event(raw)
    assert "file_path" not in ev.metadata
