from src.claude.bridge import _format_tool_params


def test_format_tool_params_keeps_cyrillic() -> None:
    out = _format_tool_params("Write", {"file_path": "проект/файл.md"})
    assert "файл.md" in out
    assert "?" not in out


def test_format_tool_params_bash_keeps_cyrillic() -> None:
    out = _format_tool_params("Bash", {"command": "echo привет"})
    assert "привет" in out
