#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


STAGES = ("business-context", "competitors", "best-posts", "market-reaction")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")
POST_ID_RE = re.compile(r"^-?\d+_\d+$")
SECRET_PATTERNS = (
    re.compile(r"vk1\.[A-Za-z0-9._-]{20,}"),
    re.compile(r"access_token\s*[:=]\s*[\"']?[A-Za-z0-9._-]{20,}", re.I),
    re.compile(r"authorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}", re.I),
)


class ChainError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise ChainError(f"missing file: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChainError(f"invalid JSON: {path.name}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_business_name(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def project_identity_from_business(
    business_context: dict[str, Any], business_context_sha256: str
) -> dict[str, str] | None:
    name = ((business_context.get("business") or {}).get("name"))
    canonical_name = canonical_business_name(name)
    if not canonical_name:
        return None
    stable_id = hashlib.sha256(canonical_name.encode("utf-8")).hexdigest()[:16]
    return {
        "project_id": f"business-{stable_id}",
        "business_name_canonical": canonical_name,
        "business_context_sha256": ensure_sha(
            business_context_sha256, "project_identity.business_context_sha256"
        ),
    }


def validate_project_identity(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ChainError(f"project identity is missing: {label}")
    name = canonical_business_name(value.get("business_name_canonical"))
    expected_id = (
        f"business-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]}"
        if name
        else ""
    )
    if value.get("project_id") != expected_id:
        raise ChainError(f"invalid project identity: {label}")
    context_sha = ensure_sha(
        value.get("business_context_sha256"),
        f"{label}.business_context_sha256",
    )
    return {
        "project_id": expected_id,
        "business_name_canonical": name,
        "business_context_sha256": context_sha,
    }


def secret_hits(value: Any) -> list[str]:
    text = value if isinstance(value, str) else canonical_bytes(value).decode("utf-8")
    return [pattern.pattern for pattern in SECRET_PATTERNS if pattern.search(text)]


def ensure_no_secrets(value: Any, label: str) -> None:
    if secret_hits(value):
        raise ChainError(f"secret detected: {label}")


def ensure_sha(value: Any, label: str) -> str:
    text = str(value or "")
    if not SHA_RE.fullmatch(text):
        raise ChainError(f"invalid sha256: {label}")
    return text


def cache_key(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()


def load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": "1.0", "entries": {}}
    value = read_json(path)
    if value.get("schema_version") != "1.0" or not isinstance(
        value.get("entries"), dict
    ):
        return {"schema_version": "1.0", "entries": {}}
    return value


def hash_file(path: Path, cache_path: Path | None = None) -> tuple[str, bool]:
    if not path.is_file():
        raise ChainError(f"missing file: {path.name}")
    stat = path.stat()
    cache = load_cache(cache_path) if cache_path else None
    key = cache_key(path)
    if cache is not None:
        entry = cache["entries"].get(key)
        if (
            isinstance(entry, dict)
            and entry.get("size") == stat.st_size
            and entry.get("mtime_ns") == stat.st_mtime_ns
            and SHA_RE.fullmatch(str(entry.get("sha256") or ""))
        ):
            return entry["sha256"], True
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if cache is not None and cache_path is not None:
        cache["entries"][key] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": value,
        }
        write_json(cache_path, cache)
    return value, False


def relative_path(path: Path, base: Path) -> str:
    value = Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    if Path(value).is_absolute():
        raise ChainError("absolute paths are forbidden")
    return value


def artifact(
    path: Path, output: Path, cache: Path | None, role: str
) -> tuple[dict[str, Any], bool]:
    digest, cached = hash_file(path, cache)
    return (
        {
            "role": role,
            "file": relative_path(path, output.parent),
            "sha256": digest,
            "bytes": path.stat().st_size,
        },
        cached,
    )


def require_status(value: dict[str, Any], label: str, allowed: set[str]) -> str:
    status = str(value.get("status") or "")
    if status not in allowed:
        raise ChainError(f"{label} has invalid status: {status or 'missing'}")
    return status


def emit_business(args: argparse.Namespace, primary: dict[str, Any]) -> dict[str, Any]:
    status = require_status(primary, "business_context.json", {"confirmed"})
    if primary.get("schema_version") not in {"1.0", "1.1"}:
        raise ChainError("unsupported business_context schema_version")
    presence = primary.get("online_presence") or {}
    competitors = primary.get("competitors") or {}
    constraints = competitors.get("search_constraints") or {}
    if isinstance(presence, dict):
        vk_url = str(presence.get("vk_url") or "")
    elif isinstance(presence, list):
        vk_url = next(
            (
                str(item.get("url") or "")
                for item in presence
                if isinstance(item, dict)
                and str(item.get("type") or "").lower().startswith("vk")
                and item.get("active", True)
            ),
            "",
        )
    else:
        vk_url = ""
    if not vk_url.startswith(("https://vk.com/", "https://vk.ru/")):
        raise ChainError("business_context has no valid VK source")
    target_count = constraints.get("target_count", constraints.get("target_total"))
    geography = constraints.get("geography")
    audience_size = constraints.get(
        "audience_size", constraints.get("vk_followers")
    )
    freshness_days = constraints.get("freshness_days")
    if freshness_days is None:
        freshness = constraints.get("freshness") or {}
        months = freshness.get("period_months") if isinstance(freshness, dict) else None
        if months is not None:
            freshness_days = int(months) * 31
    if any(
        value is None
        for value in (target_count, geography, audience_size, freshness_days)
    ):
        raise ChainError("business_context has incomplete search constraints")
    return {
        "status": status,
        "parent": None,
        "summary": {
            "vk_url": vk_url,
            "target_count": target_count,
            "geography": geography,
            "audience_size": audience_size,
            "freshness_days": freshness_days,
            "search_queries": competitors.get("proposed_search_queries") or [],
        },
    }


def emit_competitors(args: argparse.Namespace, primary: dict[str, Any]) -> dict[str, Any]:
    status = require_status(primary, "competitor_set.json", {"confirmed"})
    competitors = primary.get("competitors")
    if not isinstance(competitors, list) or not competitors:
        raise ChainError("competitor_set has no competitors")
    ids = [int(item["community_id"]) for item in competitors]
    if any(value <= 0 for value in ids) or len(ids) != len(set(ids)):
        raise ChainError("competitor_set has invalid or duplicate community_id")
    provenance = primary.get("provenance") or {}
    legacy_business = primary.get("business_context") or {}
    parent_sha = ensure_sha(
        provenance.get("business_context_sha256") or legacy_business.get("sha256"),
        "competitor_set.provenance.business_context_sha256",
    )
    return {
        "status": status,
        "parent": {"stage": "business-context", "sha256": parent_sha},
        "summary": {
            "competitor_count": len(competitors),
            "community_ids": ids,
            "communities": [
                {
                    "community_id": int(item["community_id"]),
                    "name": str(item.get("name") or ""),
                    "competitor_type": str(item.get("competitor_type") or ""),
                    "followers": int(item.get("followers") or 0),
                }
                for item in competitors
            ],
        },
    }


def post_ids_from_dataset(dataset: dict[str, Any]) -> list[str]:
    posts = dataset.get("posts")
    if not isinstance(posts, list):
        raise ChainError("dataset posts must be an array")
    values = [str(item.get("post_id") or "") for item in posts]
    if any(not POST_ID_RE.fullmatch(value) for value in values):
        raise ChainError("dataset has invalid post_id")
    if len(values) != len(set(values)):
        raise ChainError("dataset has duplicate post_id")
    return values


def emit_posts(
    args: argparse.Namespace,
    primary: dict[str, Any],
    output: Path,
    cache: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    status = require_status(primary, "vk_posts_dataset.json", {"confirmed"})
    comments = read_json(args.comments)
    receipt = read_json(args.receipt)
    ensure_no_secrets(comments, args.comments.name)
    ensure_no_secrets(receipt, args.receipt.name)
    if receipt.get("passed") is not True or receipt.get("status") != "confirmed":
        raise ChainError("best-posts receipt is not confirmed and passed")
    post_ids = post_ids_from_dataset(primary)
    if not isinstance(comments, dict) or set(post_ids) != set(comments):
        raise ChainError("dataset/comments post_id mismatch")
    files = [
        (args.primary, "dataset"),
        (args.comments, "comments"),
        (args.xlsx, "xlsx"),
        (args.receipt, "final_validation"),
    ]
    artifacts: list[dict[str, Any]] = []
    cache_hits = 0
    for path, role in files:
        item, hit = artifact(path, output, cache, role)
        artifacts.append(item)
        cache_hits += int(hit)
    by_role = {item["role"]: item for item in artifacts}
    expected = receipt.get("hashes") or {}
    aliases = {
        "dataset": ("dataset", "vk_posts_dataset.json"),
        "comments": ("comments", "vk_comments.json"),
        "xlsx": ("xlsx", "workbook", "vk_best_posts.xlsx"),
    }
    for role, keys in aliases.items():
        value = next((expected.get(key) for key in keys if expected.get(key)), None)
        if by_role[role]["sha256"] != ensure_sha(value, f"receipt.hashes.{role}"):
            raise ChainError(f"best-posts receipt hash mismatch: {role}")
    source = primary.get("source") or {}
    parent_sha = ensure_sha(
        source.get("competitor_set_sha256"),
        "dataset.source.competitor_set_sha256",
    )
    communities = primary.get("communities") or []
    return (
        {
            "status": status,
            "parent": {"stage": "competitors", "sha256": parent_sha},
            "summary": {
                "community_count": len(communities),
                "post_count": len(post_ids),
                "post_ids": post_ids,
                "window": primary.get("window"),
            },
        },
        artifacts,
        cache_hits,
    )


def emit_report(
    args: argparse.Namespace,
    primary: dict[str, Any],
    output: Path,
    cache: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    if primary.get("passed") is not True:
        raise ChainError("market-reaction validation did not pass")
    status = require_status(primary, "validation_report.json", {"draft", "confirmed"})
    files = [
        (args.primary, "validation_report"),
        (args.analysis_input, "analysis_input"),
        (args.comment_signals, "comment_signals"),
        (args.findings, "report_findings"),
        (args.html, "html"),
    ]
    artifacts: list[dict[str, Any]] = []
    cache_hits = 0
    for path, role in files:
        item, hit = artifact(path, output, cache, role)
        artifacts.append(item)
        cache_hits += int(hit)
    if args.html.suffix.lower() != ".html":
        raise ChainError("final report must be an HTML file")
    expected_html_sha = ensure_sha(
        (primary.get("output_hashes") or {}).get("html"), "report HTML"
    )
    if artifacts[-1]["sha256"] != expected_html_sha:
        raise ChainError("report HTML hash mismatch")
    source_hashes = primary.get("source_hashes") or {}
    parent = {
        role: ensure_sha(source_hashes.get(role), f"report.source_hashes.{role}")
        for role in ("dataset", "comments", "xlsx")
    }
    counts = primary.get("counts") or {}
    return (
        {
            "status": status,
            "parent": {"stage": "best-posts", "artifacts": parent},
            "summary": {
                "posts": counts.get("posts"),
                "sections": counts.get("sections"),
                "findings": counts.get("findings"),
                "evidence_blocks": counts.get("evidence_blocks"),
                "visible_words": counts.get("visible_words"),
            },
        },
        artifacts,
        cache_hits,
    )


def emit(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output
    cache = args.cache or output.parent / "hash_cache.json"
    primary = read_json(args.primary)
    ensure_no_secrets(primary, args.primary.name)
    primary_artifact, primary_cached = artifact(
        args.primary, output, cache, "primary"
    )
    artifacts = [primary_artifact]
    cache_hits = int(primary_cached)
    if args.stage == "business-context":
        detail = emit_business(args, primary)
    elif args.stage == "competitors":
        detail = emit_competitors(args, primary)
    elif args.stage == "best-posts":
        detail, artifacts, cache_hits = emit_posts(
            args, primary, output, cache
        )
    else:
        detail, artifacts, cache_hits = emit_report(
            args, primary, output, cache
        )
    identity = None
    if args.stage == "business-context":
        identity = project_identity_from_business(primary, primary_artifact["sha256"])
    else:
        upstream_path = getattr(args, "upstream_handoff", None)
        if upstream_path:
            upstream = validate_handoff(upstream_path, cache)
            expected_parent = STAGES[STAGES.index(args.stage) - 1]
            if upstream.get("stage") != expected_parent:
                raise ChainError(f"wrong upstream handoff for {args.stage}")
            identity = upstream.get("project_identity")
    result = {
        "schema_version": "1.1" if identity else "1.0",
        "stage": args.stage,
        "status": detail["status"],
        "primary": primary_artifact,
        "parent": detail["parent"],
        "artifacts": artifacts,
        "summary": detail["summary"],
        "cache": {
            "file": relative_path(cache, output.parent),
            "hits": cache_hits,
            "entries_used": len(artifacts),
        },
        "security": {"contains_secrets": False, "absolute_paths": False},
    }
    if identity:
        result["project_identity"] = validate_project_identity(identity, output.name)
    ensure_no_secrets(result, output.name)
    write_json(output, result)
    return result


def validate_handoff(path: Path, cache: Path | None = None) -> dict[str, Any]:
    value = read_json(path)
    ensure_no_secrets(value, path.name)
    schema = value.get("schema_version")
    if schema not in {"1.0", "1.1"} or value.get("stage") not in STAGES:
        raise ChainError(f"invalid handoff: {path.name}")
    if schema == "1.1":
        validate_project_identity(value.get("project_identity"), path.name)
    if value.get("security") != {
        "contains_secrets": False,
        "absolute_paths": False,
    }:
        raise ChainError(f"unsafe handoff: {path.name}")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ChainError(f"handoff has no artifacts: {path.name}")
    if value.get("stage") == "market-reaction" and not any(
        item.get("role") == "html" and str(item.get("file") or "").lower().endswith(".html")
        for item in artifacts
        if isinstance(item, dict)
    ):
        raise ChainError("final report HTML is missing from handoff")
    for item in artifacts:
        file_value = str(item.get("file") or "")
        if not file_value or Path(file_value).is_absolute():
            raise ChainError(f"handoff contains absolute path: {path.name}")
        expected = ensure_sha(
            item.get("sha256"), f"{path.name}:{item.get('role')}"
        )
        artifact_path = (path.parent / file_value).resolve()
        actual, _ = hash_file(artifact_path, cache)
        if actual != expected:
            raise ChainError(
                f"artifact hash mismatch: {path.name}:{item.get('role')}"
            )
    ensure_sha((value.get("primary") or {}).get("sha256"), f"{path.name}:primary")
    return value


def validate_transitions(
    handoffs: list[tuple[Path, dict[str, Any]]], allow_draft_report: bool
) -> None:
    if [value["stage"] for _, value in handoffs] != list(STAGES):
        raise ChainError("invalid stage order")
    business = handoffs[0][1]
    competitors = handoffs[1][1]
    posts = handoffs[2][1]
    report = handoffs[3][1]
    identities = [value.get("project_identity") for _, value in handoffs]
    if any(identity is not None for identity in identities):
        if any(identity is None for identity in identities):
            raise ChainError("project identity is missing from part of the chain")
        normalized = [
            validate_project_identity(identity, value["stage"])
            for (_, value), identity in zip(handoffs, identities)
        ]
        if any(identity != normalized[0] for identity in normalized[1:]):
            raise ChainError("project identity mismatch across handoffs")
        if normalized[0]["business_context_sha256"] != business["primary"]["sha256"]:
            raise ChainError("business project identity hash mismatch")
    for item in (business, competitors, posts):
        if item["status"] != "confirmed":
            raise ChainError(f"upstream stage is not confirmed: {item['stage']}")
    if report["status"] != "confirmed" and not allow_draft_report:
        raise ChainError("market-reaction stage is not confirmed")
    if competitors["parent"]["sha256"] != business["primary"]["sha256"]:
        raise ChainError("business-context -> competitors hash mismatch")
    if posts["parent"]["sha256"] != competitors["primary"]["sha256"]:
        raise ChainError("competitors -> best-posts hash mismatch")
    post_artifacts = {
        item["role"]: item["sha256"] for item in posts["artifacts"]
    }
    for role, expected in report["parent"]["artifacts"].items():
        if post_artifacts.get(role) != expected:
            raise ChainError(f"best-posts -> market-reaction hash mismatch: {role}")


def assemble(args: argparse.Namespace) -> dict[str, Any]:
    paths = [args.business, args.competitors, args.posts, args.report]
    cache = args.cache or args.output.parent / "hash_cache.json"
    handoffs = [(path, validate_handoff(path, cache)) for path in paths]
    validate_transitions(handoffs, args.allow_draft_report)
    stage_records = []
    cache_hits = 0
    for path, value in handoffs:
        digest, hit = hash_file(path, cache)
        cache_hits += int(hit)
        stage_records.append(
            {
                "stage": value["stage"],
                "status": value["status"],
                "handoff_file": relative_path(path, args.output.parent),
                "handoff_sha256": digest,
                "primary_sha256": value["primary"]["sha256"],
            }
        )
    identity = handoffs[0][1].get("project_identity")
    result = {
        "schema_version": "1.1" if identity else "1.0",
        "status": (
            "confirmed"
            if all(record["status"] == "confirmed" for record in stage_records)
            else "draft"
        ),
        "created_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "stages": stage_records,
        "checks": {
            "stage_order": True,
            "statuses": True,
            "parent_hashes": True,
            "post_artifact_hashes": True,
            "secrets": True,
            "relative_paths": True,
        },
        "cache": {
            "file": relative_path(cache, args.output.parent),
            "hits": cache_hits,
            "entries_used": len(stage_records),
        },
    }
    if identity:
        result["project_identity"] = identity
    ensure_no_secrets(result, args.output.name)
    write_json(args.output, result)
    return result


def validate_manifest(args: argparse.Namespace) -> dict[str, Any]:
    manifest = read_json(args.manifest)
    ensure_no_secrets(manifest, args.manifest.name)
    if manifest.get("schema_version") not in {"1.0", "1.1"}:
        raise ChainError("unsupported chain manifest")
    records = manifest.get("stages")
    if not isinstance(records, list) or len(records) != 4:
        raise ChainError("chain manifest must contain four stages")
    cache = args.cache
    if cache is None:
        cache_file = str((manifest.get("cache") or {}).get("file") or "")
        if cache_file and not Path(cache_file).is_absolute():
            cache = (args.manifest.parent / cache_file).resolve()
    handoffs = []
    for record in records:
        path = (args.manifest.parent / record["handoff_file"]).resolve()
        digest, _ = hash_file(path, cache)
        if digest != ensure_sha(record.get("handoff_sha256"), "handoff_sha256"):
            raise ChainError(f"handoff hash mismatch: {record.get('stage')}")
        value = validate_handoff(path, cache)
        if value["stage"] != record.get("stage"):
            raise ChainError("manifest/handoff stage mismatch")
        handoffs.append((path, value))
    validate_transitions(handoffs, args.allow_draft_report)
    result = {
        "passed": True,
        "status": manifest.get("status"),
        "stages": [value["stage"] for _, value in handoffs],
        "checks": manifest.get("checks"),
        "secret_scan": {"passed": True, "hits": []},
    }
    if args.output:
        write_json(args.output, result)
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)

    p = sub.add_parser("emit")
    p.add_argument("--stage", choices=STAGES, required=True)
    p.add_argument("--primary", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path)
    p.add_argument("--comments", type=Path)
    p.add_argument("--xlsx", type=Path)
    p.add_argument("--receipt", type=Path)
    p.add_argument("--analysis-input", type=Path)
    p.add_argument("--comment-signals", type=Path)
    p.add_argument("--findings", type=Path)
    p.add_argument("--html", type=Path)
    p.add_argument("--upstream-handoff", type=Path)
    p.set_defaults(func=emit)

    p = sub.add_parser("assemble")
    p.add_argument("--business", type=Path, required=True)
    p.add_argument("--competitors", type=Path, required=True)
    p.add_argument("--posts", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path)
    p.add_argument("--allow-draft-report", action="store_true")
    p.set_defaults(func=assemble)

    p = sub.add_parser("validate")
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument("--cache", type=Path)
    p.add_argument("--allow-draft-report", action="store_true")
    p.set_defaults(func=validate_manifest)
    return root


def check_required_args(args: argparse.Namespace) -> None:
    required = {
        "best-posts": ("comments", "xlsx", "receipt"),
        "market-reaction": (
            "analysis_input",
            "comment_signals",
            "findings",
            "html",
        ),
    }.get(getattr(args, "stage", ""), ())
    missing = [name for name in required if getattr(args, name, None) is None]
    if missing:
        raise ChainError("missing stage arguments: " + ", ".join(missing))


def main() -> int:
    args = parser().parse_args()
    try:
        check_required_args(args)
        result = args.func(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ChainError, OSError, KeyError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
