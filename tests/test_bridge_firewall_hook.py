"""H-1: bridge навешивает PreToolUse-firewall hook для confine'нутых сессий
и колбэк реально DENY'ит нарушающие инструменты."""
import pytest

from src.claude.bridge import ClaudeBridge


def test_no_hooks_without_confine_root() -> None:
    b = ClaudeBridge()
    assert b._build_firewall_hooks(None) is None
    assert b._build_firewall_hooks("") is None


def test_hooks_built_with_confine_root(tmp_path) -> None:
    b = ClaudeBridge()
    hooks = b._build_firewall_hooks(str(tmp_path))
    assert hooks is not None
    assert "PreToolUse" in hooks
    assert len(hooks["PreToolUse"]) >= 1


async def test_hook_callback_denies_outside_access(tmp_path) -> None:
    b = ClaudeBridge()
    hooks = b._build_firewall_hooks(str(tmp_path / "proj"))
    matcher = hooks["PreToolUse"][0]
    cb = matcher.hooks[0]
    # Чтение вне проекта → deny.
    out = await cb({"tool_name": "Read", "tool_input": {"file_path": "/etc/shadow"}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_hook_callback_allows_inside_access(tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.txt").write_text("hi")
    b = ClaudeBridge()
    hooks = b._build_firewall_hooks(str(root))
    cb = hooks["PreToolUse"][0].hooks[0]
    out = await cb(
        {"tool_name": "Read", "tool_input": {"file_path": str(root / "a.txt")}}, None, None
    )
    assert out == {}  # разрешено


async def test_hook_fail_closed_when_firewall_errors(tmp_path, monkeypatch) -> None:
    """CRITICAL: если firewall_check сам падает — хук БЛОКИРУЕТ (fail-closed),
    а не пропускает. Раньше except→{} давал fail-open (обход при любой ошибке,
    напр. shlex ValueError на кривой Bash-команде)."""
    def _boom(*a, **k):
        raise RuntimeError("firewall internal error")

    monkeypatch.setattr("src.claude.tool_firewall.firewall_check", _boom)
    b = ClaudeBridge()
    hooks = b._build_firewall_hooks(str(tmp_path))
    cb = hooks["PreToolUse"][0].hooks[0]
    out = await cb({"tool_name": "Read", "tool_input": {"file_path": "/etc/shadow"}}, None, None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_confined_session_fails_closed_on_tmux_transport(tmp_path) -> None:
    """CRITICAL: confine'нутая сессия на транспорте, где firewall навесить нельзя
    (tmux), НЕ должна запускаться без защиты — yield ERROR (fail-closed)."""
    b = ClaudeBridge(transport="tmux")
    events = []
    async for ev in b.send_message(
        message="hi", topic_id=-777, project_path=str(tmp_path), confine_root=str(tmp_path)
    ):
        events.append(ev)
    assert events, "ожидался хотя бы ERROR-event"
    assert any(getattr(e.type, "name", "") == "ERROR" for e in events)


def test_sdk_options_include_hooks_when_confined(tmp_path) -> None:
    """confine_root → опции SDK получают hooks (через настоящий ClaudeAgentOptions)."""
    sdk = pytest.importorskip("claude_agent_sdk")
    b = ClaudeBridge()
    opts = b._build_sdk_options(
        options_cls=sdk.ClaudeAgentOptions,
        project_path=str(tmp_path),
        session_id=None,
        confine_root=str(tmp_path),
    )
    assert getattr(opts, "hooks", None)


def test_build_sdk_options_fails_closed_when_hooks_unavailable(monkeypatch):
    """Confined session must NOT build options without a firewall hook."""
    import pytest
    options_cls = pytest.importorskip("claude_agent_sdk").ClaudeAgentOptions
    from src.claude.bridge import ClaudeBridge

    b = ClaudeBridge()
    monkeypatch.setattr(b, "_build_firewall_hooks", lambda confine_root: None)

    with pytest.raises(RuntimeError, match="firewall"):
        b._build_sdk_options(
            options_cls=options_cls,
            project_path=".",
            session_id=None,
            confine_root="/some/project/root",
        )


def test_build_sdk_options_ok_when_not_confined(monkeypatch):
    """No confine_root → no firewall required, options build normally."""
    import pytest
    options_cls = pytest.importorskip("claude_agent_sdk").ClaudeAgentOptions
    from src.claude.bridge import ClaudeBridge

    b = ClaudeBridge()
    monkeypatch.setattr(b, "_build_firewall_hooks", lambda confine_root: None)
    opts = b._build_sdk_options(
        options_cls=options_cls, project_path=".", session_id=None, confine_root=None
    )
    assert opts is not None


def test_fork_retry_rebuild_preserves_confine(tmp_path, monkeypatch):
    """A3: the fork-retry rebuild of SDK options for a confined session must NOT
    drop confine_root (that would produce a firewall-less, fail-open retry).

    This drives the ACTUAL retry-rebuild branch (~bridge.py:861): the topic is
    marked fork-pending so the first resume forks; the first stream attempt
    emits a fresh (forked) session_id then a transient ``stall`` error, which
    sends the retry loop into the in-place rebuild. Every ``_build_sdk_options``
    call during this confined send is recorded; none may carry confine_root=None.
    """
    import asyncio

    pytest.importorskip("claude_agent_sdk")
    from src.claude.bridge import (
        ClaudeBridge,
        ClaudeEvent,
        ClaudeEventType,
        _STREAM_DONE,
    )

    confine = str(tmp_path)
    topic_id = -999

    b = ClaudeBridge()
    b.mark_fork_pending(topic_id)  # → initial resume forks (_should_fork True)

    seen: list = []
    real_build = b._build_sdk_options

    def spy(**kwargs):
        seen.append(kwargs.get("confine_root"))
        return real_build(**kwargs)

    monkeypatch.setattr(b, "_build_sdk_options", spy)

    # Collapse the retry backoff (2**attempt s) so the test stays fast.
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay, *a, **k):
        return await real_sleep(0)

    monkeypatch.setattr("src.claude.bridge.asyncio.sleep", fast_sleep)

    calls = {"n": 0}

    async def fake_consume(*, topic_id, prompt, options, query_fn, queue, active_run):
        calls["n"] += 1
        if calls["n"] == 1:
            # Attempt 0: capture a forked session id (so the fork marker is
            # "spent"), then a transient error → retry loop rebuilds in-place.
            await queue.put(
                ClaudeEvent(
                    type=ClaudeEventType.INIT,
                    content="",
                    metadata={"session_id": "forked-sid"},
                )
            )
            await queue.put(
                ClaudeEvent(
                    type=ClaudeEventType.ERROR,
                    content="stalled",
                    metadata={"error_type": "stall"},
                )
            )
            active_run.returncode = 1
        else:
            # Attempt 1: clean finish so send_message completes.
            active_run.returncode = 0
        await queue.put(_STREAM_DONE)

    monkeypatch.setattr(b, "_consume_sdk_stream", fake_consume)

    async def drive():
        async for _ev in b.send_message(
            message="hi",
            topic_id=topic_id,
            project_path=confine,
            session_id="base-sid",
            confine_root=confine,
        ):
            pass

    asyncio.run(drive())

    assert calls["n"] >= 2, "retry-rebuild path was not reached"
    assert len(seen) >= 2, f"expected initial build + retry rebuild, got {seen}"
    assert all(
        c == confine for c in seen
    ), f"a rebuild dropped confine_root (fail-open firewall): {seen}"
