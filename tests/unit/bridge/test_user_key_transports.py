"""SP2 FIX-C — per-user ANTHROPIC_API_KEY на НЕ-SDK транспортах.

До фикса `anthropic_api_key` инъектился только на SDK-пути. CLI-fallback
(_send_message_via_cli) и tmux-путь (_send_message_via_tmux) НЕ прокидывали
ключ, поэтому USER_KEY-сессия, ушедшая на не-SDK транспорт, тихо биллила бы
owner-креды вместо ключа пользователя.

Фикс:
  * CLI-fallback: ключ прокинут в _spawn_env(...) ровно как на SDK-пути.
  * tmux: fail-closed — USER_KEY на tmux-транспорте отклоняется (ключ нельзя
    подсунуть в общий долгоживущий пане без утечки), т.к. окружение claude там
    наследуется от tmux-сервера, а не собирается _spawn_env per-message.
"""
import inspect

from src.claude.bridge import ClaudeBridge, ClaudeEventType


def _bridge(**kwargs) -> ClaudeBridge:
    return ClaudeBridge(permission_mode="bypassPermissions", **kwargs)


# --- Контракт сигнатур -------------------------------------------------------

def test_cli_fallback_has_anthropic_api_key_param() -> None:
    sig = inspect.signature(ClaudeBridge._send_message_via_cli)
    assert "anthropic_api_key" in sig.parameters
    assert sig.parameters["anthropic_api_key"].default is None


def test_tmux_has_anthropic_api_key_param() -> None:
    sig = inspect.signature(ClaudeBridge._send_message_via_tmux)
    assert "anthropic_api_key" in sig.parameters
    assert sig.parameters["anthropic_api_key"].default is None


# --- send_message прокидывает ключ в CLI-fallback ---------------------------

async def test_send_message_threads_key_into_cli_fallback(monkeypatch) -> None:
    """Когда SDK недоступен (RuntimeError) и сессия не confine'нута, send_message
    падает в CLI-fallback и обязан протащить туда per-user ключ."""
    bridge = _bridge()  # transport="sdk" по умолчанию
    monkeypatch.setattr(bridge, "_resolve_cwd", lambda p: str(p))

    def _no_sdk():
        raise RuntimeError("sdk unavailable")

    monkeypatch.setattr("src.claude.bridge._load_sdk", _no_sdk)

    captured: dict = {}

    async def _capture_cli(**kwargs):
        captured.update(kwargs)
        if False:
            yield

    monkeypatch.setattr(bridge, "_send_message_via_cli", _capture_cli)

    async for _ in bridge.send_message(
        message="hi",
        topic_id=1,
        project_path="/proj",
        anthropic_api_key="sk-ant-USER",
    ):
        pass

    assert captured["anthropic_api_key"] == "sk-ant-USER"


# --- send_message прокидывает ключ в tmux-ветку ------------------------------

async def test_send_message_passes_key_to_tmux_branch(monkeypatch) -> None:
    bridge = _bridge(transport="tmux")
    monkeypatch.setattr(bridge, "_resolve_cwd", lambda p: str(p))

    captured: dict = {}

    async def _capture_tmux(**kwargs):
        captured.update(kwargs)
        if False:
            yield

    monkeypatch.setattr(bridge, "_send_message_via_tmux", _capture_tmux)

    async for _ in bridge.send_message(
        message="hi",
        topic_id=7,
        project_path="/proj",
        anthropic_api_key="sk-ant-USER",
    ):
        pass

    assert captured["anthropic_api_key"] == "sk-ant-USER"


# --- tmux fail-closed: USER_KEY отклоняется ----------------------------------

async def test_tmux_refuses_user_key(monkeypatch) -> None:
    """USER_KEY на tmux → ERROR (fail-closed), НИ ОДНОГО вызова tmux не делаем."""
    bridge = _bridge(transport="tmux")

    async def _boom(*_a, **_k):
        raise AssertionError("tmux must NOT be touched when refusing USER_KEY")

    # Любое обращение к tmux до отказа = баг (секрет уже мог бы утечь).
    monkeypatch.setattr(bridge, "_run_tmux", _boom)
    monkeypatch.setattr(bridge, "_ensure_tmux_session", _boom)

    events = [
        ev
        async for ev in bridge._send_message_via_tmux(
            message="hi",
            topic_id=3,
            project_path="/proj",
            session_id=None,
            attachments=None,
            anthropic_api_key="sk-ant-USER",
        )
    ]

    assert len(events) == 1
    assert events[0].type == ClaudeEventType.ERROR
    assert events[0].metadata["error_type"] == "user_key_unsupported_transport"


async def test_tmux_without_key_does_not_refuse(monkeypatch) -> None:
    """Регрессия: обычная (без ключа) tmux-сессия НЕ должна отклоняться — доходит
    до реальной работы с tmux (которую здесь глушим сразу после первого вызова)."""
    bridge = _bridge(transport="tmux")

    class _StopError(Exception):
        pass

    async def _first_touch(*_a, **_k):
        raise _StopError

    monkeypatch.setattr(bridge, "_ensure_tmux_session", _first_touch)

    saw_refusal = False
    try:
        async for ev in bridge._send_message_via_tmux(
            message="hi",
            topic_id=4,
            project_path="/proj",
            session_id=None,
            attachments=None,
            anthropic_api_key=None,
        ):
            if (
                ev.type == ClaudeEventType.ERROR
                and (ev.metadata or {}).get("error_type")
                == "user_key_unsupported_transport"
            ):
                saw_refusal = True
    except _StopError:
        pass

    assert saw_refusal is False
