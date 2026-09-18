from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "resolve_workspace.py"


def load_module():
    spec = importlib.util.spec_from_file_location("resolve_agent_vk", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load resolver")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WrapperTests(unittest.TestCase):
    def test_resolves_only_agent_one_with_required_manager(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / ".agents" / "skills" / "agent-vk"
            skill.mkdir(parents=True)
            target = root / "агент 1"
            manager = target / ".agents" / "skills" / "vk-competitor-workspace" / "SKILL.md"
            manager.parent.mkdir(parents=True)
            (target / "AGENTS.md").write_text("# Agent 1\n", encoding="utf-8")
            manager.write_text("# Manager\n", encoding="utf-8")
            self.assertEqual(target.resolve(), module.resolve_workspace(skill))

    def test_neighbor_agent_cannot_substitute_for_agent_one(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            skill = root / ".agents" / "skills" / "agent-vk"
            skill.mkdir(parents=True)
            neighbor = root / "агент 2"
            neighbor.mkdir()
            (neighbor / "AGENTS.md").write_text("# Agent 2\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Agent 1"):
                module.resolve_workspace(skill)


if __name__ == "__main__":
    unittest.main()
