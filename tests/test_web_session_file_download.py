import io
import os
import zipfile
from pathlib import Path

import pytest

from src.web.routes_sessions import (
    _zip_dir_bytes,
    resolve_session_entry,
    resolve_session_file,
)


def test_resolve_returns_path_within_project(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    f = tmp_path / "docs" / "report.md"
    f.write_text("hi", encoding="utf-8")
    out = resolve_session_file(tmp_path, "docs/report.md")
    assert out == f.resolve()


def test_resolve_blocks_traversal(tmp_path: Path) -> None:
    (tmp_path.parent / "secret.txt").write_text("s", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_session_file(tmp_path, "../secret.txt")


def test_resolve_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_session_file(tmp_path, "nope.md")


# ── resolve_session_entry: то же, но допускает каталоги (для zip-скачивания) ──

def test_resolve_entry_allows_directory(tmp_path: Path) -> None:
    d = tmp_path / "lesson-copy"
    d.mkdir()
    out = resolve_session_entry(tmp_path, "lesson-copy")
    assert out == d.resolve()
    assert out.is_dir()


def test_resolve_entry_blocks_traversal_dir(tmp_path: Path) -> None:
    (tmp_path.parent / "outside").mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        resolve_session_entry(tmp_path, "../outside")


def test_resolve_entry_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_session_entry(tmp_path, "nope")


# ── _zip_dir_bytes ──

def test_zip_dir_contains_files_with_relative_arcnames(tmp_path: Path) -> None:
    d = tmp_path / "copy"
    (d / "sub").mkdir(parents=True)
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    (d / "sub" / "a.txt").write_text("a", encoding="utf-8")
    data = _zip_dir_bytes(d)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    # arcname относительно РОДИТЕЛЯ каталога → включает имя самой папки.
    assert "copy/index.html" in names
    assert "copy/sub/a.txt" in names
    assert all(not n.startswith("/") and ".." not in n for n in names)


def test_zip_dir_skips_symlinks(tmp_path: Path) -> None:
    d = tmp_path / "copy"
    d.mkdir()
    (d / "real.txt").write_text("r", encoding="utf-8")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    try:
        os.symlink(secret, d / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")
    data = _zip_dir_bytes(d)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "copy/real.txt" in names
    assert "copy/link.txt" not in names  # симлинк наружу пропущен
