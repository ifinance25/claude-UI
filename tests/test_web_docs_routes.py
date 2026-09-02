from pathlib import Path

from src.web.routes_docs import list_project_docs, read_project_doc


def test_list_project_docs_finds_md(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Readme", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")

    rels = {d["rel"] for d in list_project_docs(tmp_path)}

    assert "README.md" in rels
    assert "docs/guide.md" in rels
    assert "main.py" not in rels  # только markdown


def test_read_project_doc_returns_content(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Привет", encoding="utf-8")

    assert read_project_doc(tmp_path, "README.md") == "# Привет"


def test_read_project_doc_blocks_traversal(tmp_path: Path) -> None:
    secret = tmp_path.parent / "secret.md"
    secret.write_text("secret", encoding="utf-8")

    try:
        read_project_doc(tmp_path, "../secret.md")
        assert False, "ожидался отказ при traversal"
    except ValueError:
        pass


def test_read_project_doc_rejects_non_md(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x", encoding="utf-8")
    try:
        read_project_doc(tmp_path, "main.py")
        assert False, "ожидался отказ для не-md"
    except ValueError:
        pass
