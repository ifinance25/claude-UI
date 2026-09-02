"""Regression: the fork-retry rebuild must carry the per-user API key (bridge.py:869).

When a forked send is retried after a transient error, ``send_message`` rebuilds
the SDK options *in place* (resume the freshly-forked session id instead of
forking again). That rebuild is a second ``_build_sdk_options`` call site
(bridge.py:869-880) which — like the first one — must thread
``anthropic_api_key`` through, or the retried attempt would silently drop the
caller's key and fall back to owner credentials (a Path-B billing/isolation
leak that only manifests on the retry path, so unit coverage of the happy path
would never catch it).

This drives the real ``send_message`` retry loop with a fake SDK stream: attempt
0 emits an INIT (which captures a forked session id, so ``_should_fork`` flips to
False) then a transient ``connection`` error; attempt 1 completes cleanly. It
then asserts the retry-rebuild call site received ``anthropic_api_key`` AND that
the resulting options' env carries ``ANTHROPIC_API_KEY``.
"""
from __future__ import annotations

import src.claude.bridge as bridge_mod
from src.claude.bridge import ClaudeBridge, ClaudeEvent, ClaudeEventType, _STREAM_DONE

USER_KEY = "sk-ant-api03-fork-retry-user-key-1234"
TOPIC_ID = 4242
FORKED_SID = "forked-session-id"


class _RecordingOptions:
    """Stand-in for the SDK options class: keeps the kwargs it was built with."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _dummy_query_fn(*args, **kwargs):  # pragma: no cover - never invoked
    raise AssertionError("query_fn must not run; _consume_sdk_stream is faked")


async def test_fork_retry_rebuild_carries_user_key(monkeypatch) -> None:
    bridge = ClaudeBridge(transport="sdk", permission_mode="bypassPermissions", max_retries=2)

    # Force _should_fork(TOPIC_ID, "base-sid") -> True on the initial build.
    bridge.mark_fork_pending(TOPIC_ID)

    # Real _build_sdk_options, but backed by a recording options class and with
    # every call's kwargs captured so we can inspect the retry-rebuild site.
    monkeypatch.setattr(bridge_mod, "_load_sdk", lambda: (_RecordingOptions, _dummy_query_fn))

    build_calls: list[dict] = []
    real_build = bridge._build_sdk_options

    def recording_build(**kwargs):
        build_calls.append(kwargs)
        return real_build(**kwargs)

    bridge._build_sdk_options = recording_build

    # Avoid the real 2**attempt backoff sleep on retry.
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(bridge_mod.asyncio, "sleep", _no_sleep)

    # Fake SDK stream: attempt 0 forks (INIT) then fails transiently; attempt 1
    # completes. Records the options object handed to each attempt.
    stream_options: list = []
    attempts = {"n": 0}

    async def fake_consume(*, topic_id, prompt, options, query_fn, queue, active_run):
        attempts["n"] += 1
        stream_options.append(options)
        if attempts["n"] == 1:
            # Capture a forked session id: _session_ids[topic] set -> _should_fork
            # returns False on the retry check, triggering the in-place rebuild.
            await queue.put(
                ClaudeEvent(ClaudeEventType.INIT, metadata={"session_id": FORKED_SID})
            )
            await queue.put(
                ClaudeEvent(
                    ClaudeEventType.ERROR,
                    content="transient",
                    metadata={"error_type": "connection"},
                )
            )
        await queue.put(_STREAM_DONE)

    bridge._consume_sdk_stream = fake_consume

    events = [
        ev
        async for ev in bridge.send_message(
            message="hi",
            topic_id=TOPIC_ID,
            project_path="/x",
            session_id="base-sid",
            anthropic_api_key=USER_KEY,
        )
    ]

    # The retry actually happened (two stream attempts, a COMPLETE at the end).
    assert attempts["n"] == 2, "expected exactly one retry (two stream attempts)"
    assert any(e.type == ClaudeEventType.COMPLETE for e in events)

    # Two build sites ran: [0] initial (fork), [1] retry-rebuild (resume forked).
    assert len(build_calls) == 2, f"expected initial + retry rebuild, got {len(build_calls)}"
    retry_kwargs = build_calls[1]
    assert retry_kwargs["session_id"] == FORKED_SID
    assert retry_kwargs["fork_session"] is False
    # The regression guard: the rebuilt options MUST carry the user's key.
    assert retry_kwargs["anthropic_api_key"] == USER_KEY

    # ...and it actually lands in the spawned env of the retry attempt's options.
    retry_options = stream_options[1]
    assert retry_options.kwargs["env"]["ANTHROPIC_API_KEY"] == USER_KEY
    assert retry_options.kwargs["env"]["VELS_JAIL_NO_OWNER_CREDS"] == "1"
