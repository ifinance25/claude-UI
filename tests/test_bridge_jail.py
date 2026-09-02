import pytest


def test_confined_options_use_jail_cli_path(monkeypatch):
    options_cls = pytest.importorskip("claude_agent_sdk").ClaudeAgentOptions
    from src.claude.bridge import ClaudeBridge
    b = ClaudeBridge()
    # M-8: cli_path теперь ставится ТОЛЬКО когда bwrap/userns реально доступны.
    monkeypatch.setattr(ClaudeBridge, "_jail_available", staticmethod(lambda: True))
    monkeypatch.setattr(b, "_build_firewall_hooks", lambda confine_root: {"PreToolUse": []})
    opts = b._build_sdk_options(
        options_cls=options_cls, project_path="/proj/root",
        session_id=None, confine_root="/proj/root",
    )
    assert getattr(opts, "cli_path", None) is not None
    assert str(opts.cli_path).endswith("vels-claude-jail.sh")
    env = getattr(opts, "env", {}) or {}
    assert env.get("VELS_PROJECT_ROOT") == "/proj/root"
    assert env.get("VELS_REAL_CLAUDE")


def test_privileged_options_have_no_jail(monkeypatch):
    options_cls = pytest.importorskip("claude_agent_sdk").ClaudeAgentOptions
    from src.claude.bridge import ClaudeBridge
    b = ClaudeBridge()
    opts = b._build_sdk_options(
        options_cls=options_cls, project_path="/proj/root",
        session_id=None, confine_root=None,
    )
    assert getattr(opts, "cli_path", None) is None
    env = getattr(opts, "env", {}) or {}
    assert "VELS_PROJECT_ROOT" not in env


def test_confined_refuses_when_jail_required_but_unavailable(monkeypatch):
    options_cls = pytest.importorskip("claude_agent_sdk").ClaudeAgentOptions
    from src.claude.bridge import ClaudeBridge
    b = ClaudeBridge()
    b.require_jail = True
    monkeypatch.setattr(b, "_build_firewall_hooks", lambda confine_root: {"PreToolUse": []})
    monkeypatch.setattr(ClaudeBridge, "_jail_available", staticmethod(lambda: False))
    with pytest.raises(RuntimeError, match="jail"):
        b._build_sdk_options(
            options_cls=options_cls, project_path="/proj/root",
            session_id=None, confine_root="/proj/root",
        )


def test_confined_best_effort_when_jail_not_required(monkeypatch):
    """M-8: без require_jail и БЕЗ доступного bwrap confine'нутая сессия всё
    равно запускается (firewall-hook активен), но БЕЗ ФС-джейла — cli_path не
    ставится, иначе первое сообщение падало бы с кодом 127."""
    options_cls = pytest.importorskip("claude_agent_sdk").ClaudeAgentOptions
    from src.claude.bridge import ClaudeBridge
    b = ClaudeBridge()
    b.require_jail = False
    monkeypatch.setattr(b, "_build_firewall_hooks", lambda confine_root: {"PreToolUse": []})
    monkeypatch.setattr(ClaudeBridge, "_jail_available", staticmethod(lambda: False))
    opts = b._build_sdk_options(
        options_cls=options_cls, project_path="/proj/root",
        session_id=None, confine_root="/proj/root",
    )
    assert opts is not None
    # Ключевое: НЕ через bwrap-wrapper (иначе exec bwrap → code 127).
    assert getattr(opts, "cli_path", None) is None


def test_jail_settings_sanitized_strips_mcp_keys(tmp_path, monkeypatch):
    """M-7: копия settings.json для джейла НЕ содержит MCP-approval-ключей, но
    сохраняет остальные предпочтения (model)."""
    import json as _json

    from src.claude.bridge import ClaudeBridge

    creds = tmp_path / ".claude"
    creds.mkdir()
    (creds / "settings.json").write_text(
        _json.dumps(
            {
                "model": "sonnet",
                "enableAllProjectMcpServers": True,
                "enabledMcpjsonServers": ["google-workspace"],
                "mcpServers": {"evil": {"command": "/x"}},
            }
        ),
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("src.claude.bridge.Path.home", staticmethod(lambda: home))
    path = ClaudeBridge._jail_settings_file(creds)
    assert path is not None
    data = _json.loads(open(path, encoding="utf-8").read())
    assert data.get("model") == "sonnet"
    for key in ("enableAllProjectMcpServers", "enabledMcpjsonServers", "mcpServers"):
        assert key not in data


def test_jail_settings_none_when_absent(tmp_path):
    """Нет исходного settings.json → None (в джейл ничего не монтируем)."""
    from src.claude.bridge import ClaudeBridge

    creds = tmp_path / ".claude"
    creds.mkdir()
    assert ClaudeBridge._jail_settings_file(creds) is None
