"""Общие фикстуры тестов.

``make_mgr`` — фабрика SessionManager с гарантированным teardown
(close_sync + dispose async-engine). Без него sqlite-conn держит лок на
.db/-wal/-shm и на Windows ломает rmtree(tmp_path) с PermissionError, а
async-engine утекает между тестами (ResourceWarning). Эталон вместо копипасты
``m._engine.sync_engine.dispose()`` по ~40 файлам.
"""
from __future__ import annotations

import pytest

from src.claude.session import SessionManager


@pytest.fixture
def make_mgr(tmp_path):
    created: list[SessionManager] = []

    def _make(name: str = "s.db") -> SessionManager:
        m = SessionManager(storage_path=str(tmp_path / name))
        created.append(m)
        return m

    yield _make

    for m in created:
        try:
            m.close_sync()
            m._engine.sync_engine.dispose()
        except Exception:
            pass
