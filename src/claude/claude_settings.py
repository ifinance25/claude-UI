"""Atomic read/write of ~/.claude/settings.json — single source of truth.

Shared by the web ``/api/model`` route and the bot's ``/model`` handler so
both go through the same atomic write (tempfile + ``os.replace``) and can't
drift — previously the web side was atomic while the bot used a plain
``write_text`` that could leave a half-written, unparseable file on crash
(CR3-19).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def read_claude_settings(path: Path = CLAUDE_SETTINGS_PATH) -> dict[str, Any]:
    """Load settings.json, returning {} when absent or corrupt."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        logger.warning("claude_settings_json_invalid", error=str(exc))
        return {}


def write_claude_settings(data: dict[str, Any], path: Path = CLAUDE_SETTINGS_PATH) -> None:
    """Atomically write settings.json: temp file in the same dir + os.replace.

    Without this, a crash between truncate and flush leaves broken JSON and
    Claude fails to load settings on next start.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
