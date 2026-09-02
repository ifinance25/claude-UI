import json
import os
import stat
from src.claude.bridge import ClaudeBridge


def test_cli_args_add_strict_mcp_config(tmp_path):
    bridge = ClaudeBridge()
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"notion": {"command": "npx", "args": []}}}))
    args = bridge._build_cli_args(message="hi", session_id=None, mcp_config_path=str(cfg))
    assert "--mcp-config" in args
    assert str(cfg) in args
    assert "--strict-mcp-config" in args


def test_cli_args_no_mcp_when_absent():
    bridge = ClaudeBridge()
    args = bridge._build_cli_args(message="hi", session_id=None)
    assert "--mcp-config" not in args


def test_write_temp_mcp_config_roundtrip(tmp_path):
    bridge = ClaudeBridge()
    servers = {"notion": {"command": "npx", "args": [], "env": {"NOTION_TOKEN": "sek"}}}
    path = bridge._write_temp_mcp_config(servers)
    try:
        assert json.loads(open(path).read()) == {"mcpServers": servers}
        # POSIX only: Windows os.fchmod exists but is a no-op on perm bits,
        # so the 0600 restriction only holds (and is asserted) on POSIX.
        if os.name == "posix":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        os.unlink(path)


def test_write_temp_mcp_config_none_or_empty():
    bridge = ClaudeBridge()
    assert bridge._write_temp_mcp_config(None) is None
    assert bridge._write_temp_mcp_config({}) is None
