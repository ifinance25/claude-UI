from src.claude.bridge import ClaudeBridge


class _FakeOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_sdk_options_include_mcp_servers():
    bridge = ClaudeBridge()
    servers = {"notion": {"command": "npx", "args": [], "env": {"NOTION_TOKEN": "x"}}}
    opts = bridge._build_sdk_options(
        options_cls=_FakeOptions,
        project_path="/tmp",
        session_id=None,
        mcp_servers=servers,
    )
    assert opts.kwargs["mcp_servers"] == servers


def test_sdk_options_omit_mcp_servers_when_none():
    bridge = ClaudeBridge()
    opts = bridge._build_sdk_options(
        options_cls=_FakeOptions,
        project_path="/tmp",
        session_id=None,
        mcp_servers=None,
    )
    assert "mcp_servers" not in opts.kwargs
