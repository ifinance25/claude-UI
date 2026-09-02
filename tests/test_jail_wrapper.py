import subprocess, shutil
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "vels-claude-jail.sh"


def test_wrapper_exists():
    assert WRAPPER.exists(), "jail wrapper script missing"


def test_wrapper_shell_syntax_ok():
    sh = shutil.which("sh") or shutil.which("bash")
    if not sh:
        import pytest; pytest.skip("no POSIX shell on this host")
    r = subprocess.run([sh, "-n", str(WRAPPER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_wrapper_binds_project_not_host_secrets():
    text = WRAPPER.read_text(encoding="utf-8")
    assert "bwrap" in text
    assert "$VELS_PROJECT_ROOT" in text and "$VELS_REAL_CLAUDE" in text
    # must NOT blanket-bind host secret roots
    assert "--bind /home " not in text and "--bind /opt " not in text
    assert "--ro-bind /home " not in text and "--ro-bind /opt " not in text
    # creds mounted as read-only single file into a writable tmpfs .claude
    assert '--tmpfs "$HOME/.claude"' in text
    assert ".credentials.json" in text
