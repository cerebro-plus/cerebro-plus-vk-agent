from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
sys.path.insert(0, str(SKILLS_ROOT / "business-context-interview" / "scripts"))
sys.path.insert(0, str(SKILLS_ROOT / "business-context-interview" / "tests"))

import business_context as context  # noqa: E402
from test_business_context import sample_context  # noqa: E402


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reuse_business_context.py"


def load_module():
    spec = importlib.util.spec_from_file_location("reuse_business_context", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load reuse helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_card(directory: Path, *, name: str = "Тестовая студия", confirmed: bool = True):
    directory.parent.mkdir(parents=True, exist_ok=True)
    data = sample_context()
    data["business"]["name"] = name
    data["competitors"]["search_constraints"]["platforms"] = ["VK"]
    source = directory.parent / f"{directory.name}-input.json"
    source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    context.create_artifacts(source, directory)
    if confirmed:
        context.confirm_artifacts(directory, "2026-01-02")


class ReuseBusinessContextTests(unittest.TestCase):
    def test_imports_confirmed_previous_card_then_reuses_it(self):
        helper = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "агент 1"
            workspace.mkdir()
            old_run = root / "business-context-runs" / "2026-01-02" / "old-run"
            make_card(old_run)

            first = helper.reuse_or_import(workspace, skill_root=SKILLS_ROOT)
            self.assertEqual("imported", first["status"])
            imported_path = workspace / first["business_context_path"]
            self.assertEqual(
                (old_run / "business_context.json").read_bytes(),
                imported_path.read_bytes(),
            )
            self.assertTrue((imported_path.parent / "business_handoff.json").is_file())
            manager = helper.load_module(
                "workspace_for_test",
                SKILLS_ROOT / "vk-competitor-workspace" / "scripts" / "workspace.py",
            )
            state = manager.inspect_workspace(workspace)
            self.assertEqual(
                first["sha256"], state["business_context"]["sha256"]
            )
            route = manager.route(state, "find-competitors")
            self.assertEqual(
                ["search-vk-competitors"],
                [step["skill"] for step in route["steps"]],
            )

            second = helper.reuse_or_import(workspace, skill_root=SKILLS_ROOT)
            self.assertEqual("reused", second["status"])
            self.assertEqual(first["business_context_path"], second["business_context_path"])
            self.assertEqual(
                (old_run / "business_context.json").read_bytes(),
                imported_path.read_bytes(),
            )

    def test_draft_is_not_imported(self):
        helper = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "агент 1"
            workspace.mkdir()
            make_card(root / "business-context-runs" / "2026-01-02" / "draft", confirmed=False)

            result = helper.reuse_or_import(workspace, skill_root=SKILLS_ROOT)
            self.assertEqual("missing", result["status"])
            self.assertFalse((workspace / "runs").exists())

    def test_multiple_businesses_require_selection(self):
        helper = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "агент 1"
            workspace.mkdir()
            source = root / "business-context-runs" / "2026-01-02"
            make_card(source / "first", name="Первый бизнес")
            make_card(source / "second", name="Второй бизнес")

            result = helper.reuse_or_import(workspace, skill_root=SKILLS_ROOT)
            self.assertEqual("needs_selection", result["status"])
            self.assertEqual(2, len(result["candidates"]))
            self.assertFalse((workspace / "runs").exists())

    def test_accepts_confirmed_card_from_previous_public_skill_outside_default_folder(self):
        helper = load_module()
        previous_script = (
            REPO_ROOT
            / "public-releases"
            / "cerebro-plus-vk-business-context-skill"
            / "business-context-interview"
            / "scripts"
            / "business_context.py"
        )
        previous = helper.load_module("previous_lesson_context", previous_script)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "агент 1"
            workspace.mkdir()
            old_run = root / "previous-lesson" / "run-1"
            old_run.parent.mkdir(parents=True)
            data = sample_context()
            data["competitors"]["search_constraints"]["platforms"] = ["VK"]
            source = root / "source.json"
            source.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            previous.create_artifacts(source, old_run)
            previous.confirm_artifacts(old_run, "2026-01-02")

            result = helper.reuse_or_import(
                workspace, skill_root=SKILLS_ROOT, source_run=old_run
            )
            self.assertEqual("imported", result["status"])
            self.assertTrue((workspace / result["business_context_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
