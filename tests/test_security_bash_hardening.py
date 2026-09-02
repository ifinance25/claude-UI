import pytest

from src.bot.middleware.security import SecurityMiddleware, SecurityViolation


def _mw(tmp_path):
    return SecurityMiddleware(
        allowed_roots=[tmp_path], audit_log_path=tmp_path / "sec.log"
    )


def _bash(mw, command, root):
    mw.validate_tool_use(
        tool_name="Bash", tool_input={"command": command}, project_root=str(root)
    )


def test_blocks_shell_variable_expansion(tmp_path):
    with pytest.raises(SecurityViolation) as e:
        _bash(_mw(tmp_path), 'cat "$HOME/.claude/.credentials.json"', tmp_path)
    assert e.value.category == "shell_substitution"


def test_blocks_command_substitution(tmp_path):
    with pytest.raises(SecurityViolation) as e:
        _bash(_mw(tmp_path), "cat $(ls /etc)", tmp_path)
    assert e.value.category == "shell_substitution"


def test_blocks_backticks(tmp_path):
    with pytest.raises(SecurityViolation) as e:
        _bash(_mw(tmp_path), "echo `whoami`", tmp_path)
    assert e.value.category == "shell_substitution"


def test_blocks_inline_python(tmp_path):
    with pytest.raises(SecurityViolation) as e:
        _bash(_mw(tmp_path), "python3 -c \"print(open('/etc/passwd').read())\"", tmp_path)
    assert e.value.category == "inline_interpreter"


def test_allows_plain_command_inside_project(tmp_path):
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    _bash(_mw(tmp_path), "cat notes.txt", tmp_path)  # must NOT raise


def test_blocks_network_egress_commands(tmp_path):
    for cmd in ("curl http://x", "wget http://x", "nc 1.2.3.4 80", "ssh a@b"):
        with pytest.raises(SecurityViolation) as e:
            _bash(_mw(tmp_path), cmd, tmp_path)
        assert e.value.category == "network_command"


def test_allows_nonnetwork_command(tmp_path):
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    _bash(_mw(tmp_path), "cat f.txt", tmp_path)  # must NOT raise


def test_blocks_dev_tcp_egress(tmp_path):
    for cmd in ("exec 3<>/dev/tcp/evil.com/80", "cat </dev/udp/1.2.3.4/53"):
        with pytest.raises(SecurityViolation) as e:
            _bash(_mw(tmp_path), cmd, tmp_path)
        assert e.value.category == "network_command"
