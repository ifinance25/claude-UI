from pathlib import Path

from src.claude.skills import (
    discover_marketplace_skills,
    discover_project_skills,
    parse_skill_frontmatter,
)


def _write_skill(root: Path, name: str, desc: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def test_parse_skill_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: foo\ndescription: do foo\n---\nbody", encoding="utf-8")
    meta = parse_skill_frontmatter(f)
    assert meta == {"name": "foo", "description": "do foo"}


def test_discover_project_skills(tmp_path: Path) -> None:
    skills_dir = tmp_path / ".claude" / "skills"
    _write_skill(skills_dir, "deploy", "deploy the app")
    found = discover_project_skills(tmp_path)
    names = {s["name"] for s in found}
    assert "deploy" in names
    assert all(s["target"] == "claude" and s["cmd"].startswith("/") for s in found)


def test_discover_marketplace_skills(tmp_path: Path) -> None:
    market = tmp_path / "obra"
    _write_skill(market / "skills", "brainstorming", "explore intent")
    settings = {
        "enabledPlugins": {"superpowers@superpowers-dev": True},
        "extraKnownMarketplaces": {
            "superpowers-dev": {
                "source": {"source": "directory", "path": str(market)}
            }
        },
    }
    found = discover_marketplace_skills(settings)
    names = {s["name"] for s in found}
    assert "brainstorming" in names


def test_discover_marketplace_skills_handles_missing() -> None:
    assert discover_marketplace_skills({}) == []
