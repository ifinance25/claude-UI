"""Unit tests for Anthropic API key validation (format + live probe).

Two layers are covered:

* :func:`validate_key_format` — a pure, offline regex check. No I/O, so every
  branch is exercised directly with representative valid/invalid strings.
* :func:`probe_key` — a live ``GET /v1/models`` check. The network is never
  touched: :class:`aiohttp.ClientSession` is patched with a fake async
  context-manager so we can assert the status-code -> :class:`ProbeResult`
  mapping and that errors degrade to ``UNKNOWN`` instead of raising.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import aiohttp
import pytest

from src.apikeys.validate import (
    KEY_PATTERN,
    ProbeResult,
    probe_key,
    validate_key_format,
)

# A syntactically valid key: "sk-ant-" + 46 chars from [a-zA-Z0-9-_].
VALID_KEY = "sk-ant-api03-" + "A" * 40


# --------------------------------------------------------------------------- #
# Fake aiohttp plumbing (no real network)
# --------------------------------------------------------------------------- #
class _FakeResponse:
    """Stands in for the object returned by ``session.get(...)``.

    Acts as an async context manager. When ``exc`` is set it raises on enter
    (mirroring how aiohttp surfaces timeouts/connection errors on ``__aenter__``).
    """

    def __init__(self, status: int | None = None, exc: BaseException | None = None):
        self.status = status
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    """Stands in for :class:`aiohttp.ClientSession`."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.get_calls: list[tuple[tuple, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self._response


def _patch_session(session: _FakeSession):
    """Patch ClientSession so constructing it yields ``session``.

    Captures the constructor kwargs (e.g. ``timeout=``) on ``session.init_kwargs``.
    """

    def _factory(*args, **kwargs):
        session.init_kwargs = kwargs
        return session

    return patch("src.apikeys.validate.aiohttp.ClientSession", side_effect=_factory)


# --------------------------------------------------------------------------- #
# validate_key_format
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key",
    [
        VALID_KEY,
        "sk-ant-" + "a" * 40,
        "sk-ant-" + "Z9_-" * 12,  # mixed allowed chars, well over 40
        "sk-ant-" + "x" * 100,
    ],
)
def test_validate_key_format_accepts_valid(key):
    assert validate_key_format(key) is True


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "   ",
        "sk-ant-tooshort",  # < 40 chars after prefix
        "sk-ant-" + "a" * 39,  # exactly one short
        "sk-proj-" + "a" * 40,  # wrong prefix (OpenAI-style)
        "SK-ANT-" + "a" * 40,  # wrong case in prefix
        "ant-" + "a" * 40,  # missing sk- prefix
        "sk-ant-" + "a" * 45 + " ",  # trailing space breaks anchor
        " sk-ant-" + "a" * 45,  # leading space breaks anchor
        "sk-ant-" + "a" * 20 + "!" + "a" * 20,  # illegal char
        "sk-ant-" + "a" * 20 + "\n" + "a" * 20,  # newline
    ],
)
def test_validate_key_format_rejects_invalid(key):
    assert validate_key_format(key) is False


def test_validate_key_format_returns_bool_not_match_object():
    # Must be a real bool, not a truthy re.Match / None.
    assert validate_key_format(VALID_KEY) is True
    assert validate_key_format("nope") is False


def test_key_pattern_is_compiled_and_reusable():
    import re

    assert isinstance(KEY_PATTERN, re.Pattern)
    assert KEY_PATTERN.match(VALID_KEY)
    assert KEY_PATTERN.match("bad") is None


# --------------------------------------------------------------------------- #
# ProbeResult enum
# --------------------------------------------------------------------------- #
def test_probe_result_members():
    assert {m.name for m in ProbeResult} == {"VALID", "INVALID", "UNKNOWN"}


# --------------------------------------------------------------------------- #
# probe_key: status-code mapping
# --------------------------------------------------------------------------- #
async def test_probe_key_200_is_valid():
    session = _FakeSession(_FakeResponse(status=200))
    with _patch_session(session):
        assert await probe_key(VALID_KEY) is ProbeResult.VALID


@pytest.mark.parametrize("status", [401, 403])
async def test_probe_key_401_403_is_invalid(status):
    session = _FakeSession(_FakeResponse(status=status))
    with _patch_session(session):
        assert await probe_key(VALID_KEY) is ProbeResult.INVALID


@pytest.mark.parametrize("status", [429, 500, 502, 503, 302, 418])
async def test_probe_key_other_status_is_unknown(status):
    session = _FakeSession(_FakeResponse(status=status))
    with _patch_session(session):
        assert await probe_key(VALID_KEY) is ProbeResult.UNKNOWN


# --------------------------------------------------------------------------- #
# probe_key: error handling -> UNKNOWN (never raises)
# --------------------------------------------------------------------------- #
async def test_probe_key_timeout_is_unknown():
    session = _FakeSession(_FakeResponse(exc=asyncio.TimeoutError()))
    with _patch_session(session):
        assert await probe_key(VALID_KEY, timeout=0.01) is ProbeResult.UNKNOWN


async def test_probe_key_network_error_is_unknown():
    session = _FakeSession(_FakeResponse(exc=aiohttp.ClientConnectionError("boom")))
    with _patch_session(session):
        assert await probe_key(VALID_KEY) is ProbeResult.UNKNOWN


async def test_probe_key_generic_exception_is_unknown():
    session = _FakeSession(_FakeResponse(exc=RuntimeError("unexpected")))
    with _patch_session(session):
        assert await probe_key(VALID_KEY) is ProbeResult.UNKNOWN


# --------------------------------------------------------------------------- #
# probe_key: request shape (url, headers, timeout)
# --------------------------------------------------------------------------- #
async def test_probe_key_sends_expected_url_and_headers():
    session = _FakeSession(_FakeResponse(status=200))
    with _patch_session(session):
        await probe_key(VALID_KEY)

    assert len(session.get_calls) == 1
    args, kwargs = session.get_calls[0]
    url = args[0] if args else kwargs.get("url")
    assert url == "https://api.anthropic.com/v1/models"

    headers = kwargs["headers"]
    assert headers["x-api-key"] == VALID_KEY
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["user-agent"] == "vels-claude-bot/1.0"


async def test_probe_key_applies_timeout():
    session = _FakeSession(_FakeResponse(status=200))
    with _patch_session(session):
        await probe_key(VALID_KEY, timeout=3.5)

    # timeout is either passed to the session constructor or to get() as a
    # ClientTimeout(total=...). Accept whichever the implementation uses.
    total = None
    ctor_timeout = getattr(session, "init_kwargs", {}).get("timeout")
    if isinstance(ctor_timeout, aiohttp.ClientTimeout):
        total = ctor_timeout.total
    else:
        _, get_kwargs = session.get_calls[0]
        get_timeout = get_kwargs.get("timeout")
        if isinstance(get_timeout, aiohttp.ClientTimeout):
            total = get_timeout.total
        else:
            total = get_timeout
    assert total == 3.5
