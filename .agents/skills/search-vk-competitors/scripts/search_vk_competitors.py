from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline import (
    PipelineError,
    build_measured_candidates,
    confirm_outputs,
    finalize_outputs,
    load_json,
    search_config,
    sha256_file,
    utc_now_iso,
    validate_business_context,
    validate_output_directory,
    write_json,
)
from vk_api import (
    RetryPolicy,
    VKAPIError,
    VKClient,
    dedupe_candidates,
    enrich_groups,
    resolve_reference_groups,
)
from workflow import (
    WorkflowError,
    build_review_batches,
    candidate_rank,
    discover_with_saturation,
    generated_query_plan,
    known_inputs,
    merge_review_documents,
    next_review_batch,
    normalize_context,
    prefilter_candidates,
    save_review_batches,
    validate_review_document,
)


def parse_window_end(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PipelineError("--window-end must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_token(token_file: str | None) -> str:
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("VK_API_TOKEN", "").strip()
    if not token:
        raise PipelineError(
            "Provide --token-file or the VK_API_TOKEN environment variable"
        )
    return token


def run_collect(args: argparse.Namespace) -> int:
    context_path = Path(args.business_context)
    output_path = Path(args.output)
    review_dir = Path(args.review_dir)
    if output_path.exists() or review_dir.exists():
        raise PipelineError(f"Refusing to overwrite {output_path}")
    context = load_json(context_path)
    errors = validate_business_context(context)
    if errors:
        raise PipelineError("; ".join(errors))
    config = search_config(context)
    token = load_token(args.token_file)
    retry = RetryPolicy(
        attempts=args.retry_attempts,
        base_delay_seconds=args.retry_base_delay,
        max_delay_seconds=args.retry_max_delay,
    )
    client = VKClient(
        token,
        api_version=args.api_version,
        retry_policy=retry,
        timeout_seconds=args.timeout,
    )
    query_plan = generated_query_plan(context)
    api_preflight = client.preflight([
        ("groups.search", {"q": query_plan[0]["query"], "count": 1, "offset": 0})
    ])
    discovered, search_audit = discover_with_saturation(
        client.call,
        query_plan,
        pool_target=config["pool_target"],
    )
    reference_inputs = known_inputs(context)
    for url in args.include_vk_url or []:
        reference_inputs.append(
            {
                "name": "",
                "url": url,
                "origin": "required_inclusion",
            }
        )
    resolved, resolution_audit = resolve_reference_groups(
        client.call,
        reference_inputs,
    )
    candidates = enrich_groups(
        client.call,
        dedupe_candidates([*discovered, *resolved]),
    )
    candidates, prefilter_audit = prefilter_candidates(candidates, config)
    for candidate in candidates:
        candidate["rank_score"] = candidate_rank(candidate, config)
    candidates.sort(key=lambda item: int(item.get("id", 0)))
    eligible = [
        item
        for item in candidates
        if (item.get("prefilter") or {}).get("passed") is True
    ]

    for reference in resolution_audit:
        if reference.get("resolved_community_ids") or not reference.get("name"):
            continue
        matched_ids = [
            int(candidate["id"])
            for candidate in candidates
            if reference["name"] in candidate.get("matched_queries", [])
        ]
        if matched_ids:
            reference["resolved_community_ids"] = matched_ids
            reference["status"] = "found_by_groups_search"

    total_batches = (
        (len(eligible) + args.review_batch_size - 1) // args.review_batch_size
        if eligible
        else 0
    )
    raw = {
        "schema_version": "1.1",
        "generated_at": utc_now_iso(),
        "provenance": {
            "business_context_file": "business_context.json",
            "business_context_sha256": sha256_file(context_path),
        },
        "criteria_snapshot": {
            "target_count": config["target_count"],
            "pool_target": config["pool_target"],
            "followers_min": config["followers_min"],
            "followers_max": config["followers_max"],
            "followers_inclusive": config["followers_inclusive"],
            "geography": config["primary_geography"],
            "activity_freshness_hours": config["activity_freshness_hours"],
            "metric_window_hours": 31 * 24,
            "required_inclusions": config["required_inclusions"],
            "exclusions": config["exclusions"],
            "key_features": config["key_features"],
            "matching_policy": config["matching_policy"],
        },
        "vk_api_version": args.api_version,
        "vk_api_preflight": api_preflight,
        "vk_methods": [
            "utils.resolveScreenName",
            "groups.search",
            "groups.getById",
        ],
        "search": {
            "query_plan": query_plan,
            **search_audit,
        },
        "pagination": {
            "groups_search_page_size": 1000,
        },
        "known_and_required_resolution": resolution_audit,
        "prefilter_audit": prefilter_audit,
        "semantic_batches": {
            "batch_size": args.review_batch_size,
            "eligible_candidates": len(eligible),
            "total_batches": total_batches,
            "maximum_batch_size": 200,
        },
        "retry_policy": {
            "attempts": retry.attempts,
            "base_delay_seconds": retry.base_delay_seconds,
            "max_delay_seconds": retry.max_delay_seconds,
        },
        "vk_api_calls": client.calls,
        "errors": client.errors,
        "limitations": [
            (
                f"{item['origin']} not resolved to a VK community: "
                f"{item.get('name') or item.get('url')}"
            )
            for item in resolution_audit
            if not item.get("resolved_community_ids")
        ],
        "candidates": candidates,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, raw)
    raw_hash = sha256_file(output_path)
    manifest, batches = build_review_batches(
        context,
        eligible,
        business_context_sha256=sha256_file(context_path),
        raw_candidates_sha256=raw_hash,
        batch_size=args.review_batch_size,
    )
    save_review_batches(
        review_dir,
        manifest,
        batches,
        writer=write_json,
    )
    print(
        json.dumps(
            {
                "output": str(output_path),
                "candidates": len(candidates),
                "eligible_candidates": len(eligible),
                "review_batches": len(batches),
                "search_stop_reason": search_audit["stop_reason"],
                "vk_api_calls": client.calls,
                "errors": len(raw["errors"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_init_review(args: argparse.Namespace) -> int:
    batch = load_json(Path(args.batch))
    output = Path(args.output)
    if output.exists():
        raise PipelineError(f"Refusing to overwrite {output}")
    checked_at = None
    candidates = []
    for item in batch.get("candidates", []):
        candidates.append(
            {
                "community_id": int(item["community_id"]),
                "decision": None,
                "competitor_type": None,
                "geography_match": False,
                "geography": "",
                "site": None,
                "assortment": "",
                "audience": "",
                "price_segment": "",
                "sales_model": "",
                "production": "",
                "online_offline": "",
                "reason": "",
                "exclusion_code": None,
                "classification_evidence": {
                    "same_primary_product": False,
                    "same_audience": False,
                    "same_specialization": False,
                    "primary_offer_coverage_ratio": 0,
                    "partial_or_similar_assortment": False,
                    "similar_need": False,
                    "substitute_or_complement": False,
                },
                "key_feature_check": {
                    "policy_applies": False,
                    "matches": None,
                    "matched": [],
                    "missing": [],
                    "rationale": "",
                },
                "constraint_check": {
                    "matched_required_inclusion": bool(
                        item.get("required_inclusion")
                    ),
                    "matched_exclusions": [],
                },
                "source_checks": [
                    {
                        "url": item["vk"],
                        "kind": "official_vk",
                        "official": True,
                        "checked_at": checked_at,
                        "supports": [],
                    }
                ],
                "priority_score": float(item.get("rank_score") or 0),
            }
        )
    document = {
        "schema_version": "1.1",
        "generated_at": utc_now_iso(),
        "batch_index": int(batch["batch_index"]),
        "processed_batches": [int(batch["batch_index"])],
        "candidates": candidates,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, document)
    print(
        json.dumps(
            {"output": str(output), "candidates": len(candidates)},
            ensure_ascii=False,
        )
    )
    return 0


def run_merge_reviews(args: argparse.Namespace) -> int:
    directory = Path(args.reviews_dir)
    files = sorted(directory.glob("review-*.json"))
    if not files:
        raise PipelineError("No review-*.json files found")
    output = Path(args.output)
    if output.exists():
        raise PipelineError(f"Refusing to overwrite {output}")
    documents = [load_json(path) for path in files]
    merged = merge_review_documents(documents)
    processed = merged["processed_batches"]
    if processed != list(range(1, len(processed) + 1)):
        raise PipelineError("review batches must be processed sequentially from 1")
    context = load_json(Path(args.business_context))
    raw = load_json(Path(args.raw_candidates))
    config = normalize_context(context)
    candidates_by_id = {
        int(item.get("id") or item.get("community_id")): item
        for item in raw.get("candidates", [])
    }
    errors = validate_review_document(
        merged,
        candidates_by_id=candidates_by_id,
        config=config,
    )
    if errors:
        raise PipelineError("; ".join(errors))
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, merged)
    print(
        json.dumps(
            {
                "output": str(output),
                "reviews": len(merged["candidates"]),
                "processed_batches": processed,
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_measure(args: argparse.Namespace) -> int:
    context_path = Path(args.business_context)
    raw_path = Path(args.raw_candidates)
    reviews_path = Path(args.candidate_reviews)
    output = Path(args.output)
    if output.exists():
        raise PipelineError(f"Refusing to overwrite {output}")
    context = load_json(context_path)
    errors = validate_business_context(context)
    if errors:
        raise PipelineError("; ".join(errors))
    raw = load_json(raw_path)
    reviews = load_json(reviews_path)
    previous = load_json(Path(args.previous_measured)) if args.previous_measured else None
    token = load_token(args.token_file)
    retry = RetryPolicy(
        attempts=args.retry_attempts,
        base_delay_seconds=args.retry_base_delay,
        max_delay_seconds=args.retry_max_delay,
    )
    client = VKClient(
        token,
        api_version=args.api_version,
        retry_policy=retry,
        timeout_seconds=args.timeout,
    )
    candidate_rows = raw.get("candidates") or []
    if candidate_rows:
        first_id = int(candidate_rows[0].get("id") or candidate_rows[0].get("community_id"))
        client.preflight([("wall.get", {"owner_id": -first_id, "count": 1, "offset": 0})])
    document = build_measured_candidates(
        context,
        raw,
        reviews,
        context_hash=sha256_file(context_path),
        raw_hash=sha256_file(raw_path),
        reviews_hash=sha256_file(reviews_path),
        call=client.call,
        window_end=parse_window_end(args.window_end),
        api_version=args.api_version,
        retry_policy={
            "attempts": retry.attempts,
            "base_delay_seconds": retry.base_delay_seconds,
            "max_delay_seconds": retry.max_delay_seconds,
        },
        previous=previous,
    )
    document["vk_api_calls"] = client.calls
    document["errors"] = [*client.errors, *document.get("errors", [])]
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, document)
    print(
        json.dumps(
            {
                "output": str(output),
                "candidates": len(document["candidates"]),
                "wall_calls": client.calls,
                "errors": len(document["errors"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_review_status(args: argparse.Namespace) -> int:
    context = load_json(Path(args.business_context))
    config = normalize_context(context)
    manifest = load_json(Path(args.manifest))
    reviews = load_json(Path(args.candidate_reviews))
    included_ids = {
        int(item["community_id"])
        for item in reviews.get("candidates", [])
        if item.get("decision") == "include"
    }
    qualified_count = len(included_ids)
    if args.measured_candidates:
        measured = load_json(Path(args.measured_candidates))
        end = parse_window_end(measured["activity_window"]["end"])
        start_epoch = int(
            (end - timedelta(hours=config["activity_freshness_hours"])).timestamp()
        )
        qualified_count = 0
        for candidate in measured.get("candidates", []):
            community_id = int(
                candidate.get("id") or candidate.get("community_id") or 0
            )
            wall = candidate.get("wall") or {}
            latest = wall.get("latest_own_publication_epoch")
            if (
                community_id in included_ids
                and isinstance(latest, int)
                and latest >= start_epoch
                and wall.get("stop_reason") != "vk_api_error"
            ):
                qualified_count += 1
    next_batch = next_review_batch(
        manifest,
        reviews,
        qualified_count=qualified_count,
        target_count=config["target_count"],
    )
    processed = set(reviews.get("processed_batches") or [])
    total_batches = int(manifest.get("total_batches") or 0)
    result = {
        "schema_version": "1.1",
        "target_count": config["target_count"],
        "qualified_count": qualified_count,
        "processed_batches": sorted(processed),
        "total_batches": total_batches,
        "next_batch": next_batch,
        "target_met": qualified_count >= config["target_count"],
        "search_exhausted": len(processed) >= total_batches,
        "shortfall": (
            qualified_count < config["target_count"]
            and len(processed) >= total_batches
        ),
    }
    if args.output:
        output = Path(args.output)
        if output.exists():
            raise PipelineError(f"Refusing to overwrite {output}")
        write_json(output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def run_finalize(args: argparse.Namespace) -> int:
    target_met = finalize_outputs(
        Path(args.business_context),
        Path(args.raw_candidates),
        Path(args.measured_candidates),
        Path(args.candidate_reviews),
        Path(args.output_dir),
    )
    print(
        json.dumps(
            {
                "output_dir": args.output_dir,
                "target_met": target_met,
                "status": "draft",
            },
            ensure_ascii=False,
        )
    )
    return 0 if target_met else 2


def run_validate(args: argparse.Namespace) -> int:
    errors = validate_output_directory(
        Path(args.directory),
        Path(args.business_context) if args.business_context else None,
    )
    if errors:
        print(json.dumps({"status": "failed", "errors": errors}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "passed", "errors": []}, ensure_ascii=False))
    return 0


def run_confirm(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    errors = validate_output_directory(
        directory,
        Path(args.business_context) if args.business_context else None,
    )
    if errors:
        raise PipelineError("Cannot confirm invalid result: " + "; ".join(errors))
    confirm_outputs(directory, args.date)
    errors = validate_output_directory(
        directory,
        Path(args.business_context) if args.business_context else None,
    )
    if errors:
        raise PipelineError("Confirmation validation failed: " + "; ".join(errors))
    print(
        json.dumps(
            {"status": "confirmed", "confirmation_date": args.date},
            ensure_ascii=False,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Search and validate VK competitors from confirmed business context"
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--business-context", required=True)
    collect.add_argument("--output", required=True)
    collect.add_argument("--review-dir", required=True)
    collect.add_argument("--token-file")
    collect.add_argument("--api-version", default="5.199")
    collect.add_argument("--retry-attempts", type=int, default=5)
    collect.add_argument("--retry-base-delay", type=float, default=0.5)
    collect.add_argument("--retry-max-delay", type=float, default=8.0)
    collect.add_argument("--timeout", type=float, default=30.0)
    collect.add_argument("--review-batch-size", type=int, default=200)
    collect.add_argument(
        "--include-vk-url",
        action="append",
        default=[],
        help="Resolve and include an explicit user-provided VK community URL.",
    )
    collect.set_defaults(handler=run_collect)

    init_review = subparsers.add_parser("init-review")
    init_review.add_argument("--batch", required=True)
    init_review.add_argument("--output", required=True)
    init_review.set_defaults(handler=run_init_review)

    merge_reviews = subparsers.add_parser("merge-reviews")
    merge_reviews.add_argument("--business-context", required=True)
    merge_reviews.add_argument("--raw-candidates", required=True)
    merge_reviews.add_argument("--reviews-dir", required=True)
    merge_reviews.add_argument("--output", required=True)
    merge_reviews.set_defaults(handler=run_merge_reviews)

    measure = subparsers.add_parser("measure")
    measure.add_argument("--business-context", required=True)
    measure.add_argument("--raw-candidates", required=True)
    measure.add_argument("--candidate-reviews", required=True)
    measure.add_argument("--output", required=True)
    measure.add_argument("--previous-measured")
    measure.add_argument("--token-file")
    measure.add_argument("--window-end")
    measure.add_argument("--api-version", default="5.199")
    measure.add_argument("--retry-attempts", type=int, default=5)
    measure.add_argument("--retry-base-delay", type=float, default=0.5)
    measure.add_argument("--retry-max-delay", type=float, default=8.0)
    measure.add_argument("--timeout", type=float, default=30.0)
    measure.set_defaults(handler=run_measure)

    review_status = subparsers.add_parser("review-status")
    review_status.add_argument("--business-context", required=True)
    review_status.add_argument("--manifest", required=True)
    review_status.add_argument("--candidate-reviews", required=True)
    review_status.add_argument("--measured-candidates")
    review_status.add_argument("--output")
    review_status.set_defaults(handler=run_review_status)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--business-context", required=True)
    finalize.add_argument("--raw-candidates", required=True)
    finalize.add_argument("--measured-candidates", required=True)
    finalize.add_argument("--candidate-reviews", required=True)
    finalize.add_argument("--output-dir", required=True)
    finalize.set_defaults(handler=run_finalize)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--directory", required=True)
    validate.add_argument("--business-context")
    validate.set_defaults(handler=run_validate)

    confirm = subparsers.add_parser("confirm")
    confirm.add_argument("--directory", required=True)
    confirm.add_argument("--date", required=True)
    confirm.add_argument("--business-context")
    confirm.set_defaults(handler=run_confirm)

    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        PipelineError,
        WorkflowError,
        VKAPIError,
        OSError,
        ValueError,
    ) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
