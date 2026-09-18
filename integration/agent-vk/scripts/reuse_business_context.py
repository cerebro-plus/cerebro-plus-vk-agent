"""Reuse a confirmed business card from Agent 1 or the earlier interview lesson."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path
from typing import Any


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _modules(skill_root: Path):
    context = load_module(
        "agent1_business_context",
        skill_root / "business-context-interview" / "scripts" / "business_context.py",
    )
    manager = load_module(
        "agent1_workspace",
        skill_root / "vk-competitor-workspace" / "scripts" / "workspace.py",
    )
    chain = load_module(
        "agent1_pipeline_chain",
        skill_root / "_shared" / "scripts" / "pipeline_chain.py",
    )
    return context, manager, chain


def _candidate(directory: Path, context, chain) -> dict[str, Any] | None:
    if context.validate_directory(directory):
        return None
    primary = directory / "business_context.json"
    data = json.loads(primary.read_text(encoding="utf-8"))
    if data.get("status") != "confirmed":
        return None
    name = " ".join(str((data.get("business") or {}).get("name") or "").split())
    vk_url = str((data.get("online_presence") or {}).get("vk_url") or "")
    return {
        "directory": directory,
        "name": name,
        "vk_url": vk_url,
        "identity": (chain.canonical_business_name(name), vk_url.casefold()),
        "sha256": hashlib.sha256(primary.read_bytes()).hexdigest(),
        "confirmation_date": data.get("confirmation_date") or "",
    }


def reuse_or_import(
    workspace: Path,
    *,
    skill_root: Path | None = None,
    source_run: Path | None = None,
) -> dict[str, Any]:
    workspace = workspace.resolve()
    skill_root = skill_root or workspace / ".agents" / "skills"
    context, manager, chain = _modules(skill_root)
    roots = [workspace / "runs", workspace / "business-context-runs", workspace.parent / "business-context-runs"]
    if source_run is not None:
        directories = [source_run.resolve()]
    else:
        directories = []
        for root in roots:
            if root.is_dir():
                directories.extend(path.parent for path in root.rglob("business_context.json"))
    candidates = []
    seen = set()
    for directory in directories:
        directory = directory.resolve()
        if directory in seen:
            continue
        seen.add(directory)
        candidate = _candidate(directory, context, chain)
        if candidate:
            candidates.append(candidate)
    if not candidates:
        return {"status": "missing", "message": "No confirmed business card was found."}

    identities = {item["identity"] for item in candidates}
    if len(identities) > 1:
        return {
            "status": "needs_selection",
            "candidates": [
                {"name": item["name"], "vk_url": item["vk_url"], "confirmation_date": item["confirmation_date"], "source_run": str(item["directory"])}
                for item in candidates
            ],
        }

    candidates.sort(
        key=lambda item: (
            item["confirmation_date"],
            item["directory"].stat().st_mtime_ns,
        ),
        reverse=True,
    )
    chosen = candidates[0]
    own_runs = workspace / "runs"
    local = [item for item in candidates if item["directory"].is_relative_to(own_runs)]
    # An already valid local run remains the preferred source when the same
    # confirmed card appears in both the previous lesson and Agent 1.
    if local and local[0]["sha256"] == chosen["sha256"]:
        chosen = local[0]
    source_dir = chosen["directory"]
    handoff = source_dir / "business_handoff.json"
    if source_dir.is_relative_to(own_runs) and handoff.is_file():
        try:
            chain.validate_handoff(handoff)
        except (chain.ChainError, OSError, ValueError):
            pass
        else:
            return {
                "status": "reused",
                "business_context_path": manager.rel(source_dir / "business_context.json", workspace),
                "sha256": chosen["sha256"],
                "business_name": chosen["name"],
            }

    manifest = manager.create_run(workspace, "business-context-interview", chosen["sha256"])
    destination = workspace / manifest["run_path"]
    shutil.copyfile(source_dir / "business_context.json", destination / "business_context.json")
    shutil.copyfile(source_dir / "business_card.md", destination / "business_card.md")
    chain.emit(argparse.Namespace(
        stage="business-context",
        primary=destination / "business_context.json",
        output=destination / "business_handoff.json",
        cache=None,
    ))
    return {
        "status": "imported",
        "business_context_path": manager.rel(destination / "business_context.json", workspace),
        "sha256": chosen["sha256"],
        "business_name": chosen["name"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--source-run", type=Path)
    args = parser.parse_args()
    result = reuse_or_import(args.workspace_root, source_run=args.source_run)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"reused", "imported", "missing", "needs_selection"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
