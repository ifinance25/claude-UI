import asyncio
from pathlib import Path

from src.web.routes_model import build_slash_commands, build_slash_commands_async


def _write_skill(root: Path, name: str, desc: str) -> None:
    d = root / ".claude" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n", encoding="utf-8"
    )


def test_build_includes_builtin_and_project_skills(tmp_path: Path) -> None:
    _write_skill(tmp_path, "deploy", "deploy it")
    result = build_slash_commands(project_path=str(tmp_path), claude_settings={})
    cmds = {c["cmd"] for c in result}
    assert "/model" in cmds  # встроенная
    assert "/deploy" in cmds  # скилл проекта
    deploy = next(c for c in result if c["cmd"] == "/deploy")
    assert deploy["kind"] == "skill"


def test_build_without_project_returns_builtin() -> None:
    result = build_slash_commands(project_path=None, claude_settings={})
    assert any(c["cmd"] == "/model" for c in result)


def test_async_merges_cli_commands_without_dupes(tmp_path, monkeypatch) -> None:
    import src.web.routes_model as rm

    async def fake_fetch(project_path):
        return ["model", "cost", "pr-comments"]

    monkeypatch.setattr(rm, "fetch_cli_commands_for_project", fake_fetch)
    result = asyncio.run(
        build_slash_commands_async(project_path=str(tmp_path), claude_settings={})
    )
    cmds = [c["cmd"] for c in result]
    assert "/pr-comments" in cmds  # новая из CLI
    assert cmds.count("/model") == 1  # не задвоилась со встроенной
    assert cmds.count("/cost") == 1
