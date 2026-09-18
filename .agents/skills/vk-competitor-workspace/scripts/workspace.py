#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = SKILL_ROOT.parent
POLICY_PATH = SKILL_ROOT / "references" / "routing-policy.json"
REQUIRED_SKILLS = (
    "business-context-interview",
    "search-vk-competitors",
    "analyze-vk-best-posts",
    "vk-content-report",
    "vk-competitor-workspace",
)
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,79}$")
SECRET_PATTERNS = (
    re.compile(r"vk1\.[A-Za-z0-9._-]{20,}"),
    re.compile(r"access_token\s*[:=]\s*[\"']?[A-Za-z0-9._-]{20,}", re.I),
    re.compile(r"authorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}", re.I),
)
BUSINESS_DEPENDENT = {
    "find-competitors",
    "update-competitors",
    "weekly-report",
    "market-report",
}


class WorkspaceError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise WorkspaceError(f"missing file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"invalid JSON: {path.name}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def ensure_sha(value: Any, label: str) -> str:
    text = str(value or "")
    if not SHA_RE.fullmatch(text):
        raise WorkspaceError(f"invalid sha256: {label}")
    return text


def contains_secret(value: Any) -> bool:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def cache_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": "1.0", "entries": {}}
    try:
        value = read_json(path)
    except WorkspaceError:
        return {"schema_version": "1.0", "entries": {}}
    if value.get("schema_version") != "1.0" or not isinstance(
        value.get("entries"), dict
    ):
        return {"schema_version": "1.0", "entries": {}}
    return value


def hash_file(path: Path, cache_path: Path) -> tuple[str, bool]:
    if not path.is_file():
        raise WorkspaceError(f"missing file: {path}")
    stat = path.stat()
    cache = load_cache(cache_path)
    key = cache_key(path)
    cached = cache["entries"].get(key)
    if (
        isinstance(cached, dict)
        and cached.get("size") == stat.st_size
        and cached.get("mtime_ns") == stat.st_mtime_ns
        and SHA_RE.fullmatch(str(cached.get("sha256") or ""))
    ):
        return cached["sha256"], True
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    cache["entries"][key] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": value,
    }
    write_json(cache_path, cache)
    return value, False


def rel(path: Path, root: Path) -> str:
    value = Path(os.path.relpath(path.resolve(), root.resolve())).as_posix()
    if Path(value).is_absolute():
        raise WorkspaceError("absolute path in compact output")
    return value


def latest(paths: list[Path]) -> list[Path]:
    return sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def business_record(path: Path, root: Path, cache: Path) -> dict[str, Any]:
    value = read_json(path)
    if contains_secret(value):
        raise WorkspaceError("business context contains secret")
    if value.get("status") != "confirmed":
        raise WorkspaceError("business context is not confirmed")
    if value.get("schema_version") not in {None, "1.0", "1.1"}:
        raise WorkspaceError("unsupported business context schema")
    digest, cached = hash_file(path, cache)
    return {
        "valid": True,
        "status": "confirmed",
        "path": rel(path, root),
        "sha256": digest,
        "schema_version": value.get("schema_version", "legacy"),
        "confirmation_date": value.get("confirmation_date"),
        "hash_cache_hit": cached,
    }


def business_parent_sha(value: dict[str, Any]) -> str | None:
    provenance = value.get("provenance") or {}
    legacy = value.get("business_context") or {}
    return provenance.get("business_context_sha256") or legacy.get("sha256")


def competitor_record(
    path: Path,
    root: Path,
    cache: Path,
    business: dict[str, Any] | None,
) -> dict[str, Any]:
    value = read_json(path)
    if contains_secret(value):
        raise WorkspaceError("competitor set contains secret")
    if value.get("status") != "confirmed":
        raise WorkspaceError("competitor set is not confirmed")
    competitors = value.get("competitors")
    if not isinstance(competitors, list) or not competitors:
        raise WorkspaceError("competitor set is empty")
    parent_sha = ensure_sha(business_parent_sha(value), "business parent")
    if business and parent_sha != business["sha256"]:
        raise WorkspaceError("competitor set belongs to another business context")
    digest, cached = hash_file(path, cache)
    return {
        "valid": True,
        "status": "confirmed",
        "path": rel(path, root),
        "sha256": digest,
        "business_context_sha256": parent_sha,
        "competitor_count": len(competitors),
        "confirmation_date": value.get("confirmation_date"),
        "hash_cache_hit": cached,
    }


def receipt_hash(receipt: dict[str, Any], role: str) -> str:
    aliases = {
        "dataset": ("dataset", "vk_posts_dataset.json"),
        "comments": ("comments", "vk_comments.json"),
        "xlsx": ("xlsx", "workbook", "vk_best_posts.xlsx"),
    }
    hashes = receipt.get("hashes") or {}
    value = next((hashes.get(key) for key in aliases[role] if hashes.get(key)), None)
    return ensure_sha(value, f"receipt.{role}")


def best_posts_record(
    receipt_path: Path,
    root: Path,
    cache: Path,
    competitor: dict[str, Any] | None,
) -> dict[str, Any]:
    receipt = read_json(receipt_path)
    if contains_secret(receipt):
        raise WorkspaceError("best-post receipt contains secret")
    if receipt.get("passed") is not True or receipt.get("status") != "confirmed":
        raise WorkspaceError("best-post receipt is not confirmed and passed")
    output_dir = receipt_path.parent
    files = {
        "dataset": output_dir / "vk_posts_dataset.json",
        "comments": output_dir / "vk_comments.json",
        "xlsx": output_dir / "vk_best_posts.xlsx",
    }
    actual: dict[str, str] = {}
    hits = 0
    for role, path in files.items():
        digest, hit = hash_file(path, cache)
        hits += int(hit)
        if digest != receipt_hash(receipt, role):
            raise WorkspaceError(f"best-post artifact hash mismatch: {role}")
        actual[role] = digest
    dataset = read_json(files["dataset"])
    if dataset.get("status") != "confirmed":
        raise WorkspaceError("best-post dataset is not confirmed")
    parent_sha = ensure_sha(
        (dataset.get("source") or {}).get("competitor_set_sha256"),
        "dataset competitor parent",
    )
    if competitor and parent_sha != competitor["sha256"]:
        raise WorkspaceError("best posts belong to another competitor set")
    duration = int((dataset.get("window") or {}).get("duration_hours") or 0)
    return {
        "valid": True,
        "status": "confirmed",
        "receipt_path": rel(receipt_path, root),
        "dataset_path": rel(files["dataset"], root),
        "comments_path": rel(files["comments"], root),
        "xlsx_path": rel(files["xlsx"], root),
        "receipt_sha256": hash_file(receipt_path, cache)[0],
        "dataset_sha256": actual["dataset"],
        "comments_sha256": actual["comments"],
        "xlsx_sha256": actual["xlsx"],
        "competitor_set_sha256": parent_sha,
        "duration_hours": duration,
        "post_count": (receipt.get("counts") or {}).get("posts"),
        "hash_cache_hits": hits,
    }


def report_record(
    path: Path,
    root: Path,
    cache: Path,
    best: dict[str, Any] | None,
) -> dict[str, Any]:
    value = read_json(path)
    if contains_secret(value):
        raise WorkspaceError("report validation contains secret")
    if value.get("passed") is not True:
        raise WorkspaceError("report validation did not pass")
    status = str(value.get("status") or "")
    if status not in {"draft", "confirmed"}:
        raise WorkspaceError("report validation has invalid status")
    sources = value.get("source_hashes") or {}
    for role in ("dataset", "comments", "xlsx"):
        ensure_sha(sources.get(role), f"report source {role}")
    if best and any(
        sources[role] != best[f"{role}_sha256"]
        for role in ("dataset", "comments", "xlsx")
    ):
        raise WorkspaceError("report belongs to another best-post dataset")
    html_path = path.parent / "vk_content_report.html"
    if not html_path.is_file():
        raise WorkspaceError("report HTML is missing")
    expected_html_sha = ensure_sha(
        (value.get("output_hashes") or {}).get("html"), "report HTML"
    )
    html_sha, _ = hash_file(html_path, cache)
    if html_sha != expected_html_sha:
        raise WorkspaceError("report HTML hash mismatch")
    return {
        "valid": True,
        "status": status,
        "confirmed": status == "confirmed",
        "path": rel(path, root),
        "html_path": rel(html_path, root),
        "html_sha256": html_sha,
        "sha256": hash_file(path, cache)[0],
        "source_hashes": {
            role: sources[role] for role in ("dataset", "comments", "xlsx")
        },
        "counts": value.get("counts") or {},
    }


def first_valid(
    paths: list[Path],
    builder,
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    for path in latest(paths):
        try:
            return builder(path)
        except (WorkspaceError, KeyError, TypeError, ValueError) as exc:
            errors.append({"file": path.name, "reason": str(exc)})
    return None


def inspect_workspace(root: Path, output: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    runs = root / "runs"
    cache = runs / ".vk_workspace_hash_cache.json"
    errors: dict[str, list[dict[str, str]]] = {
        "business_context": [],
        "competitor_set": [],
        "best_posts": [],
        "market_report": [],
    }
    business = first_valid(
        list(runs.rglob("business_context.json")) if runs.exists() else [],
        lambda path: business_record(path, root, cache),
        errors["business_context"],
    )
    competitor = first_valid(
        list(runs.rglob("competitor_set.json")) if runs.exists() else [],
        lambda path: competitor_record(path, root, cache, business),
        errors["competitor_set"],
    )
    best = first_valid(
        list(runs.rglob("final_validation.json")) if runs.exists() else [],
        lambda path: best_posts_record(path, root, cache, competitor),
        errors["best_posts"],
    )
    report = first_valid(
        list(runs.rglob("validation_report.json")) if runs.exists() else [],
        lambda path: report_record(path, root, cache, best),
        errors["market_report"],
    )
    state = {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "workspace_root": ".",
        "business_context": business,
        "competitor_set": competitor,
        "best_posts": best,
        "market_report": report,
        "invalid_candidates": errors,
        "security": {
            "contains_secrets": False,
            "absolute_paths": False,
            "token_files_read": False,
        },
    }
    if output:
        write_json(output, state)
    return state


def load_policy() -> dict[str, Any]:
    policy = read_json(POLICY_PATH)
    if policy.get("schema_version") != "1.0":
        raise WorkspaceError("unsupported routing policy")
    return policy["stages"]


def stage_config(
    policy: dict[str, Any], skill: str, analysis_mode: str
) -> dict[str, str]:
    value = policy[skill]
    if skill == "vk-content-report":
        value = value[analysis_mode]
    return {"model": value["model"], "effort": value["effort"]}


def step(
    policy: dict[str, Any],
    skill: str,
    analysis_mode: str,
    condition: str,
    inputs: dict[str, Any],
) -> dict[str, Any]:
    return {
        "skill": skill,
        **stage_config(policy, skill, analysis_mode),
        "condition": condition,
        "inputs": inputs,
    }


def route(
    state: dict[str, Any],
    intent: str,
    *,
    period_hours: int = 168,
    analysis_mode: str = "regular",
    previous_stage_status: str = "none",
    degraded_media: bool = False,
    degraded_consent: bool = False,
) -> dict[str, Any]:
    if period_hours <= 0:
        raise WorkspaceError("period_hours must be positive")
    policy = load_policy()
    result: dict[str, Any] = {
        "schema_version": "1.0",
        "intent": intent,
        "status": "planned",
        "period_hours": period_hours,
        "steps": [],
        "reuse": [],
        "block_reason": None,
    }
    if previous_stage_status in {"failed", "blocked"}:
        result.update(status="blocked", block_reason="previous_stage_failed")
        return result
    if degraded_media and not degraded_consent:
        result.update(status="blocked", block_reason="degraded_media_requires_consent")
        return result
    business = state.get("business_context")
    if intent in BUSINESS_DEPENDENT and not (business and business.get("valid")):
        result["steps"] = [
            step(
                policy,
                "business-context-interview",
                analysis_mode,
                "business_context_missing_invalid_or_unconfirmed",
                {},
            )
        ]
        result["block_reason"] = "business_context_required"
        return result
    if intent == "update-context":
        result["steps"] = [
            step(
                policy,
                "business-context-interview",
                analysis_mode,
                "explicit_context_update",
                {},
            )
        ]
        return result
    if intent == "inspect":
        result["status"] = "reuse"
        return result
    competitor = state.get("competitor_set")
    if intent in {"find-competitors", "update-competitors"}:
        if (
            intent == "find-competitors"
            and competitor
            and competitor.get("valid")
            and competitor.get("business_context_sha256") == business.get("sha256")
        ):
            result["status"] = "reuse"
            result["reuse"] = ["competitor_set"]
            return result
        result["steps"] = [
            step(
                policy,
                "search-vk-competitors",
                analysis_mode,
                "explicit_competitor_search_request",
                {
                    "business_context_path": business["path"],
                    "business_context_sha256": business["sha256"],
                    "force_refresh": intent == "update-competitors",
                },
            )
        ]
        return result
    if intent == "market-report":
        best = state.get("best_posts")
        if not (best and best.get("valid")):
            result.update(status="blocked", block_reason="best_posts_required")
            return result
        result["steps"] = [
            step(
                policy,
                "vk-content-report",
                analysis_mode,
                "best_posts_validation_and_media_gate_passed",
                {
                    "dataset_path": best["dataset_path"],
                    "comments_path": best["comments_path"],
                    "xlsx_path": best["xlsx_path"],
                    "receipt_path": best["receipt_path"],
                },
            )
        ]
        return result
    if intent != "weekly-report":
        result.update(status="blocked", block_reason="unsupported_intent")
        return result
    if not (competitor and competitor.get("valid")):
        result.update(status="blocked", block_reason="missing_competitor_set")
        return result
    best = state.get("best_posts")
    reusable_best = bool(
        best
        and best.get("valid")
        and best.get("competitor_set_sha256") == competitor.get("sha256")
        and int(best.get("duration_hours") or 0) == period_hours
    )
    if reusable_best:
        result["reuse"].append("best_posts")
        report = state.get("market_report")
        if (
            report
            and report.get("valid")
            and report.get("source_hashes", {}).get("dataset")
            == best.get("dataset_sha256")
        ):
            if report.get("confirmed"):
                result["status"] = "reuse"
                result["reuse"].append("market_report")
                return result
            result["status"] = "awaiting_confirmation"
            result["reuse"].append("market_report_draft")
            return result
        result["steps"] = [
            step(
                policy,
                "vk-content-report",
                analysis_mode,
                "best_posts_validation_and_media_gate_passed",
                {
                    "dataset_path": best["dataset_path"],
                    "comments_path": best["comments_path"],
                    "xlsx_path": best["xlsx_path"],
                    "receipt_path": best["receipt_path"],
                },
            )
        ]
        return result
    result["steps"] = [
        step(
            policy,
            "analyze-vk-best-posts",
            analysis_mode,
            "competitor_set_confirmed",
            {
                "competitor_set_path": competitor["path"],
                "competitor_set_sha256": competitor["sha256"],
                "period_hours": period_hours,
                "allow_degraded_media": degraded_media,
                "degraded_consent_recorded": degraded_consent,
            },
        ),
        step(
            policy,
            "vk-content-report",
            analysis_mode,
            "previous_stage_passed_and_media_gate_passed",
            {"input_from": "analyze-vk-best-posts.posts_handoff.json"},
        ),
    ]
    return result


def create_run(
    root: Path,
    stage: str,
    input_sha256: str,
    *,
    run_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    ensure_sha(input_sha256, "input")
    date_value = date or dt.datetime.now().astimezone().date().isoformat()
    dt.date.fromisoformat(date_value)
    identifier = run_id or (
        dt.datetime.now().strftime("%H%M%S")
        + "-"
        + stage
        + "-"
        + secrets.token_hex(4)
    )
    if not RUN_ID_RE.fullmatch(identifier):
        raise WorkspaceError("invalid run-id")
    run_dir = root.resolve() / "runs" / date_value / identifier
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise WorkspaceError("run directory already exists") from exc
    manifest = {
        "schema_version": "1.0",
        "status": "created",
        "stage": stage,
        "run_path": rel(run_dir, root.resolve()),
        "input_sha256": input_sha256,
        "model": model,
        "effort": effort,
        "created_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "security": {"contains_secrets": False, "absolute_paths": False},
    }
    write_json(run_dir / "run_manifest.json", manifest)
    return manifest


def check_workspace(root: Path, output: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    issues: list[str] = []
    agents = root / "AGENTS.md"
    if not agents.is_file():
        issues.append("missing AGENTS.md")
    else:
        text = agents.read_text(encoding="utf-8")
        for skill in REQUIRED_SKILLS:
            if f"${skill}" not in text:
                issues.append(f"AGENTS.md does not reference ${skill}")
    for skill in REQUIRED_SKILLS:
        if not (root / ".agents" / "skills" / skill / "SKILL.md").is_file():
            issues.append(f"missing project skill: {skill}")
    policy = load_policy()
    expected = {
        "business-context-interview",
        "search-vk-competitors",
        "analyze-vk-best-posts",
        "vk-content-report",
    }
    if set(policy) != expected:
        issues.append("routing policy has unexpected stages")
    if not (SKILLS_ROOT / "_shared" / "scripts" / "pipeline_chain.py").is_file():
        issues.append("missing shared chain validator")
    result = {
        "passed": not issues,
        "issues": issues,
        "skills_root": ".agents/skills",
        "required_skills": list(REQUIRED_SKILLS),
        "security": {"contains_secrets": False, "absolute_paths": False},
    }
    if output:
        write_json(output, result)
    if issues:
        raise WorkspaceError("; ".join(issues))
    return result


def shared_chain_module():
    path = SKILLS_ROOT / "_shared" / "scripts" / "pipeline_chain.py"
    spec = importlib.util.spec_from_file_location("project_pipeline_chain", path)
    if spec is None or spec.loader is None:
        raise WorkspaceError("cannot load shared chain validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_chain(
    manifest: Path,
    output: Path | None,
    allow_draft_report: bool,
) -> dict[str, Any]:
    module = shared_chain_module()
    try:
        return module.validate_manifest(
            argparse.Namespace(
                manifest=manifest,
                output=output,
                cache=None,
                allow_draft_report=allow_draft_report,
            )
        )
    except module.ChainError as exc:
        raise WorkspaceError(str(exc)) from exc


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check")
    p.add_argument("--workspace-root", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p = sub.add_parser("inspect")
    p.add_argument("--workspace-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("route")
    p.add_argument("--state", type=Path, required=True)
    p.add_argument(
        "--intent",
        required=True,
        choices=(
            "inspect",
            "update-context",
            "find-competitors",
            "update-competitors",
            "weekly-report",
            "market-report",
        ),
    )
    p.add_argument("--period-hours", type=int, default=168)
    p.add_argument(
        "--analysis-mode",
        choices=("first_complex", "regular"),
        default="regular",
    )
    p.add_argument(
        "--previous-stage-status",
        choices=("none", "passed", "failed", "blocked"),
        default="none",
    )
    p.add_argument("--degraded-media", action="store_true")
    p.add_argument("--degraded-consent", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("create-run")
    p.add_argument("--workspace-root", type=Path, required=True)
    p.add_argument("--stage", choices=REQUIRED_SKILLS, required=True)
    p.add_argument("--input-sha256", required=True)
    p.add_argument("--run-id")
    p.add_argument("--model")
    p.add_argument("--effort")
    p.add_argument("--date")
    p = sub.add_parser("validate-chain")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument("--allow-draft-report", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "check":
            result = check_workspace(args.workspace_root, args.output)
        elif args.command == "inspect":
            result = inspect_workspace(args.workspace_root, args.output)
        elif args.command == "route":
            state = read_json(args.state)
            result = route(
                state,
                args.intent,
                period_hours=args.period_hours,
                analysis_mode=args.analysis_mode,
                previous_stage_status=args.previous_stage_status,
                degraded_media=args.degraded_media,
                degraded_consent=args.degraded_consent,
            )
            write_json(args.output, result)
        elif args.command == "create-run":
            result = create_run(
                args.workspace_root,
                args.stage,
                args.input_sha256,
                run_id=args.run_id,
                model=args.model,
                effort=args.effort,
                date=args.date,
            )
        else:
            result = validate_chain(
                args.manifest,
                args.output,
                args.allow_draft_report,
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkspaceError, OSError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
