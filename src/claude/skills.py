"""Обнаружение установленных скиллов Claude Code для отображения в UI.

Источники:
  1. Скиллы проекта: <project>/.claude/skills/<name>/SKILL.md
  2. Скиллы включённых плагинов: из ~/.claude/settings.json
     (enabledPlugins + extraKnownMarketplaces[*].source.path)/skills/*/SKILL.md

Каждый скилл маппится в slash-команду {cmd, label, target="claude", kind="skill"}.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()

_MAX_SKILLS = 300


def parse_skill_frontmatter(skill_md: Path) -> dict[str, str]:
    """Достаёт name/description из YAML-frontmatter SKILL.md.

    Минимальный парсер (без PyYAML): берём блок между первыми '---'
    и читаем строки 'key: value'. Frontmatter скиллов плоский.
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in ("name", "description") and value:
            out[key] = value
    return out


def _skill_entry(meta: dict[str, str], fallback_name: str) -> dict[str, str]:
    name = meta.get("name") or fallback_name
    desc = meta.get("description") or ""
    # Короткая подпись для меню — первая фраза описания.
    label = desc.split(".")[0][:80] if desc else name
    return {
        "cmd": f"/{name}",
        "label": label,
        "target": "claude",
        "kind": "skill",
        "name": name,
    }


def _scan_skills_dir(skills_dir: Path) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not skills_dir.is_dir():
        return out
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        meta = parse_skill_frontmatter(skill_md)
        out.append(_skill_entry(meta, child.name))
        if len(out) >= _MAX_SKILLS:
            break
    return out


def discover_project_skills(project_path: Path) -> list[dict[str, str]]:
    return _scan_skills_dir(Path(project_path) / ".claude" / "skills")


def discover_marketplace_skills(settings: dict[str, Any]) -> list[dict[str, str]]:
    """Скиллы включённых плагинов по данным settings.json."""
    enabled = settings.get("enabledPlugins") or {}
    markets = settings.get("extraKnownMarketplaces") or {}
    # Имена маркетплейсов, у которых включён хотя бы один плагин.
    active_markets = {
        spec.split("@", 1)[1]
        for spec, on in enabled.items()
        if on and "@" in spec
    }
    out: list[dict[str, str]] = []
    for market_name, market in markets.items():
        if active_markets and market_name not in active_markets:
            continue
        path = (((market or {}).get("source") or {}).get("path"))
        if not path:
            continue
        out.extend(_scan_skills_dir(Path(path) / "skills"))
        if len(out) >= _MAX_SKILLS:
            break
    return out
