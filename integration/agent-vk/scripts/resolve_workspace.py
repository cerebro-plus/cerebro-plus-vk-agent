from __future__ import annotations

import json
from pathlib import Path


def resolve_workspace(skill_dir: Path) -> Path:
    root = skill_dir.resolve().parents[2]
    target = (root / "агент 1").resolve()
    if not (target / "AGENTS.md").is_file():
        raise ValueError("Agent 1 AGENTS.md is missing")
    manager = target / ".agents" / "skills" / "vk-competitor-workspace" / "SKILL.md"
    if not manager.is_file():
        raise ValueError("Agent 1 managing skill is missing")
    return target


def main() -> int:
    try:
        target = resolve_workspace(Path(__file__).resolve().parents[1])
    except ValueError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ready", "workspace": str(target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
