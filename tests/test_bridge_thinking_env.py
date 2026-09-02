import os

from src.claude.bridge import ClaudeBridge


def test_spawn_env_sets_utf8_and_drops_proxy() -> None:
    os.environ["HTTP_PROXY"] = "http://example:8080"
    try:
        bridge = ClaudeBridge(permission_mode="bypassPermissions")
        env = bridge._spawn_env()
        assert env.get("PYTHONIOENCODING") == "utf-8"
        assert env.get("PYTHONUTF8") == "1"
        assert "HTTP_PROXY" not in env
    finally:
        os.environ.pop("HTTP_PROXY", None)


def test_extended_thinking_enabled_by_default() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions")
    env = bridge._spawn_env()
    assert env.get("MAX_THINKING_TOKENS")


def test_extended_thinking_can_be_disabled() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions", extended_thinking=False)
    env = bridge._spawn_env()
    assert "MAX_THINKING_TOKENS" not in env


class _DummyOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_sdk_options_enable_thinking_summarized() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions")
    opt = bridge._build_sdk_options(
        options_cls=_DummyOptions, project_path="/x", session_id=None
    )
    assert opt.kwargs["thinking"] == {
        "type": "enabled",
        "budget_tokens": 8000,
        "display": "summarized",
    }


def test_sdk_options_no_thinking_when_disabled() -> None:
    bridge = ClaudeBridge(permission_mode="bypassPermissions", extended_thinking=False)
    opt = bridge._build_sdk_options(
        options_cls=_DummyOptions, project_path="/x", session_id=None
    )
    assert "thinking" not in opt.kwargs
