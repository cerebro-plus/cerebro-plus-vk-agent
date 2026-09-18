#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from common import (
    InputValidationError,
    MediaGateError,
    SkillError,
    load_json,
    normalize_moscow_timestamp,
    sha256_file,
    validate_competitor_set,
    write_json,
)
from media_pipeline import prepare_media_environment
from pipeline import (
    collect_week,
    confirm_outputs,
    create_json_outputs,
    process_dataset,
    validate_outputs,
)
from vk_api import VkClient
from xlsx_writer import build_workbook, check_xlsx_environment, prepare_node_modules
from schema_validator import validate_schema


SCRIPT_DIR = Path(__file__).resolve().parent
REFERENCE_DIR = SCRIPT_DIR.parent / "references"


def token_value(args: argparse.Namespace) -> str:
    if getattr(args, "token_file", None):
        value = args.token_file.read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get("VK_API_TOKEN", "").strip()
    if not value:
        raise InputValidationError("VK token is required through env or --token-file")
    return value


def preflight(args: argparse.Namespace) -> None:
    raw = args.competitor_set.read_text(encoding="utf-8")
    source = load_json(args.competitor_set)
    validate_schema(
        source,
        REFERENCE_DIR / "competitor-set.schema.json",
        "competitor_set",
    )
    competitors = validate_competitor_set(source, raw)
    source_hash = sha256_file(args.competitor_set)
    existing_path = args.run_dir / "preflight.json"
    existing = load_json(existing_path) if existing_path.is_file() else {}
    if existing and existing.get("competitor_set_sha256") != source_hash:
        raise InputValidationError(
            "preflight.json belongs to a different competitor_set.json"
        )
    started_at = normalize_moscow_timestamp(
        args.started_at or existing.get("started_at")
    )
    if existing.get("started_at") and normalize_moscow_timestamp(
        str(existing["started_at"])
    ) != started_at:
        raise InputValidationError("The fixed run start cannot be changed")
    report = prepare_media_environment(args.run_dir)
    token = token_value(args)
    api_client = VkClient(token)
    report["vk_api"] = api_client.preflight([
        (
            "wall.get",
            {
                "owner_id": -competitors[0]["community_id"],
                "filter": "owner",
                "count": 1,
                "offset": 0,
            },
        )
    ])
    report["xlsx_environment"] = check_xlsx_environment(args.node, args.node_modules)
    prepare_node_modules(args.run_dir, args.node_modules)
    report["competitor_set"] = args.competitor_set.name
    report["competitor_set_sha256"] = source_hash
    report["started_at"] = started_at
    report["timezone"] = "Europe/Moscow"
    write_json(args.run_dir / "preflight.json", report)


def collect(args: argparse.Namespace) -> None:
    preflight_path = args.run_dir / "preflight.json"
    if not preflight_path.is_file():
        raise InputValidationError("preflight.json is required before collect")
    preflight_report = load_json(preflight_path)
    if preflight_report.get("competitor_set_sha256") != sha256_file(
        args.competitor_set
    ):
        raise InputValidationError("competitor_set.json changed after preflight")
    started_at = normalize_moscow_timestamp(str(preflight_report.get("started_at") or ""))
    if args.started_at and normalize_moscow_timestamp(args.started_at) != started_at:
        raise InputValidationError("--started-at differs from the fixed preflight time")
    token = token_value(args)
    client = VkClient(token)
    try:
        collect_week(args.competitor_set, token, args.run_dir, started_at, client=client)
    except SkillError:
        write_json(
            args.run_dir / "collection_error.json",
            {
                "status": "blocked",
                "started_at": started_at,
                "api_calls": client.calls,
                "retries": client.retries,
                "errors": client.errors,
                "token_included": False,
            },
        )
        raise


def process(args: argparse.Namespace) -> None:
    token = token_value(args)
    process_dataset(
        args.run_dir,
        VkClient(token),
        allow_degraded=args.allow_degraded_media,
        degraded_consent=args.degraded_consent,
    )
    create_json_outputs(args.run_dir)


def build(args: argparse.Namespace) -> None:
    build_workbook(args.run_dir, args.node, args.node_modules, SCRIPT_DIR / "build_workbook.mjs")


def validate(args: argparse.Namespace) -> None:
    token = token_value(args) if args.token_file or os.environ.get("VK_API_TOKEN") else None
    validate_outputs(args.competitor_set, args.run_dir, literal_token=token)


def confirm(args: argparse.Namespace) -> None:
    confirm_outputs(args.run_dir, args.confirmed_at)
    build(args)
    validate(args)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command", required=True)
    shared_input = argparse.ArgumentParser(add_help=False)
    shared_input.add_argument("--competitor-set", required=True, type=Path)
    shared_input.add_argument("--run-dir", required=True, type=Path)
    shared_token = argparse.ArgumentParser(add_help=False)
    shared_token.add_argument("--token-file", type=Path)

    p = sub.add_parser("preflight", parents=[shared_input, shared_token])
    p.add_argument("--started-at")
    p.add_argument("--node", required=True, type=Path)
    p.add_argument("--node-modules", required=True, type=Path)
    p.set_defaults(func=preflight)
    p = sub.add_parser("collect", parents=[shared_input, shared_token])
    p.add_argument("--started-at")
    p.set_defaults(func=collect)
    p = sub.add_parser("process", parents=[shared_token])
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--allow-degraded-media", action="store_true")
    p.add_argument("--degraded-consent")
    p.set_defaults(func=process)
    p = sub.add_parser("build")
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--node", required=True, type=Path)
    p.add_argument("--node-modules", required=True, type=Path)
    p.set_defaults(func=build)
    p = sub.add_parser("validate", parents=[shared_input, shared_token])
    p.set_defaults(func=validate)
    p = sub.add_parser("confirm", parents=[shared_input, shared_token])
    p.add_argument("--confirmed-at", required=True)
    p.add_argument("--node", required=True, type=Path)
    p.add_argument("--node-modules", required=True, type=Path)
    p.set_defaults(func=confirm)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.func(args)
        return 0
    except (SkillError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
