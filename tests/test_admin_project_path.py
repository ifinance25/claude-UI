from pathlib import Path

import pytest
from fastapi import HTTPException

from src.web.routes_admin import _resolve_project_abspath


def test_relative_name_joins_projects_root(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    assert _resolve_project_abspath("CRM", root) == str((root / "CRM").resolve())


def test_slash_crm_does_not_use_filesystem_root(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    assert _resolve_project_abspath("/CRM", root) == str((root / "CRM").resolve())


def test_rejects_nested_escape(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    with pytest.raises(HTTPException) as ei:
        _resolve_project_abspath("../etc", root)
    assert ei.value.status_code == 400
