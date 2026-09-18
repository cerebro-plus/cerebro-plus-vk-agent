from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from common import (
    InputValidationError,
    MediaGateError,
    VkApiError,
    load_json,
    parse_timestamp,
    scan_secret_files,
    sha256_file,
    utc_iso,
    validate_competitor_set,
    write_json,
)
from schema_validator import validate_schema
from xlsx_contract import inspect_xlsx
from xlsx_writer import (
    BEST_COLUMNS,
    CHECKED_COLUMNS,
    EXPECTED_SHEETS,
    UNAVAILABLE_COLUMNS,
)
from media_pipeline import (
    cached_download,
    download_confirmed_clip,
    evaluate_media_gate,
    image_candidates,
    make_collage,
    normalize_image,
    prepare_media_environment,
    transcribe_clip,
)
from metrics import (
    CLIP_TYPES,
    apply_benchmarks,
    calculate_er,
    classify_post,
    giveaway_flag,
    poll_flag,
    verify_video_attachment,
)
from vk_api import API_VERSION, VkClient


REFERENCE_DIR = Path(__file__).resolve().parent.parent / "references"


def normalize_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "comment_id": str(comment.get("id") or ""),
        "from_id": str(comment.get("from_id") or ""),
        "date": (
            dt.datetime.fromtimestamp(int(comment["date"]), dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if comment.get("date")
            else None
        ),
        "text": str(comment.get("text") or ""),
        "likes": int((comment.get("likes") or {}).get("count") or 0),
        "deleted": bool(comment.get("deleted")),
    }


def resolve_video(client: VkClient, video: dict[str, Any]) -> dict[str, Any] | None:
    owner_id, video_id = int(video.get("owner_id") or 0), int(video.get("id") or 0)
    if not owner_id or not video_id:
        return None
    key = f"{owner_id}_{video_id}"
    if video.get("access_key"):
        key += f"_{video['access_key']}"
    try:
        response = client.call("video.get", {"videos": key, "extended": 0})
    except VkApiError:
        return None
    items = (response or {}).get("items") or []
    return items[0] if items else None


def normalize_post(
    client: VkClient, competitor: dict[str, Any], raw: dict[str, Any]
) -> dict[str, Any]:
    owner_id, post_id_num = int(raw["owner_id"]), int(raw["id"])
    attachments = list(raw.get("attachments") or [])
    video_attachments = [
        item
        for item in attachments
        if str(item.get("type") or "").lower() in ({"video"} | CLIP_TYPES)
    ]
    verified_videos = [
        verify_video_attachment(item, resolver=lambda value: resolve_video(client, value))
        for item in video_attachments
    ]
    post_type = classify_post(attachments, verified_videos)
    likes = int((raw.get("likes") or {}).get("count") or 0)
    comments = int((raw.get("comments") or {}).get("count") or 0)
    reposts = int((raw.get("reposts") or {}).get("count") or 0)
    views_raw = (raw.get("views") or {}).get("count")
    views = int(views_raw) if views_raw is not None else 0
    giveaway, giveaway_evidence = giveaway_flag(str(raw.get("text") or ""))
    return {
        "post_id": f"{owner_id}_{post_id_num}",
        "owner_id": owner_id,
        "vk_post_id": post_id_num,
        "community_id": competitor["community_id"],
        "community_name": competitor["name"],
        "community_vk": competitor["vk"],
        "competitor_type": competitor["competitor_type"],
        "followers": competitor["followers"],
        "date": (
            dt.datetime.fromtimestamp(int(raw["date"]), dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "timestamp": int(raw["date"]),
        "url": f"https://vk.com/wall{owner_id}_{post_id_num}",
        "text": str(raw.get("text") or ""),
        "post_type": post_type,
        "poll": poll_flag(attachments),
        "giveaway": giveaway,
        "giveaway_evidence": giveaway_evidence,
        "likes": likes,
        "comments": comments,
        "reposts": reposts,
        "views": views,
        "er": calculate_er(likes, comments, reposts, views),
        "er_status": "calculated" if views > 0 else "not_calculated_no_views",
        "is_pinned": bool(raw.get("is_pinned")),
        "attachments": attachments,
        "verified_videos": verified_videos,
        "media": {
            "images": [],
            "collage_path": None,
            "collage_width": None,
            "collage_height": None,
            "clip_path": None,
            "transcription_status": None,
            "transcript": None,
        },
        "data_status": "complete",
    }


def deduplicate_normalized_posts(
    posts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate cross-community wall items by their canonical VK post ID.

    A post may be returned in more than one included community feed. When one
    copy belongs to the post owner's own community, keep that attribution.
    """
    chosen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    duplicates = 0
    for post in posts:
        key = str(post["post_id"])
        current = chosen.get(key)
        if current is None:
            chosen[key] = post
            order.append(key)
            continue
        duplicates += 1
        current_is_owner = abs(int(current["owner_id"])) == int(
            current["community_id"]
        )
        candidate_is_owner = abs(int(post["owner_id"])) == int(
            post["community_id"]
        )
        if candidate_is_owner and not current_is_owner:
            chosen[key] = post
    return [chosen[key] for key in order], duplicates


def collect_week(
    competitor_set_path: Path,
    token: str,
    run_dir: Path,
    started_at: str,
    client: VkClient | None = None,
) -> dict[str, Any]:
    raw_text = competitor_set_path.read_text(encoding="utf-8")
    source = json.loads(raw_text)
    competitors = validate_competitor_set(source, raw_text)
    end_local = parse_timestamp(started_at)
    end_utc = end_local.astimezone(dt.timezone.utc)
    start_utc = end_utc - dt.timedelta(hours=168)
    start_ts, end_ts = int(start_utc.timestamp()), int(end_utc.timestamp())
    client = client or VkClient(token)
    posts = []
    comments: dict[str, Any] = {}
    pagination = {}
    unavailable: list[dict[str, Any]] = []
    for competitor in competitors:
        wall, wall_audit = client.wall_window(
            competitor["community_id"], start_ts, end_ts
        )
        normalized = []
        for raw in wall:
            owner_id = int(raw.get("owner_id") or -int(competitor["community_id"]))
            vk_post_id = int(raw.get("id") or 0)
            post_url = f"https://vk.com/wall{owner_id}_{vk_post_id}"
            try:
                post = normalize_post(client, competitor, raw)
            except InputValidationError as exc:
                unavailable.append(
                    {
                        "object": f"Пост {owner_id}_{vk_post_id}",
                        "link": post_url,
                        "reason": str(exc),
                        "scope": "post_classification",
                        "post_id": f"{owner_id}_{vk_post_id}",
                    }
                )
                continue
            if post["comments"]:
                raw_comments, comment_audit = client.all_comments(
                    post["owner_id"], post["vk_post_id"]
                )
            else:
                raw_comments, comment_audit = [], {
                    "top_level_expected": 0,
                    "top_level_collected": 0,
                    "top_level_pages": 0,
                    "replies_expected": 0,
                    "replies_collected": 0,
                    "reply_pages": 0,
                }
            top = []
            for raw_comment in raw_comments:
                item = normalize_comment(raw_comment)
                item["replies"] = [
                    normalize_comment(reply)
                    for reply in raw_comment.get("_collected_replies") or []
                ]
                top.append(item)
            comments[post["post_id"]] = {
                "post_id": post["post_id"],
                "post_url": post["url"],
                "api_post_comment_count": post["comments"],
                "top_level_comments": top,
                "audit": comment_audit,
            }
            normalized.append(post)
            posts.append(post)
        pagination[str(competitor["community_id"])] = {
            "wall": wall_audit,
            "posts": {
                post["post_id"]: comments[post["post_id"]]["audit"]
                for post in normalized
            },
        }
    posts, global_duplicates = deduplicate_normalized_posts(posts)
    dataset = {
        "schema_version": "1.0",
        "status": "draft",
        "run_started_at": started_at,
        "timezone": "Europe/Moscow",
        "window": {
            "start": utc_iso(start_utc),
            "end": utc_iso(end_utc),
            "duration_hours": 168,
            "inclusion": "start <= post.date <= end",
        },
        "source": {
            "competitor_set_path": competitor_set_path.name,
            "competitor_set_sha256": sha256_file(competitor_set_path),
            "competitor_set_status": source.get("status"),
            "competitor_count": len(competitors),
            "new_competitor_search_performed": False,
        },
        "method": {
            "vk_api_version": API_VERSION,
            "er_formula": "(likes + comments + reposts) / views",
            "benchmark": "mean valid ER per community in fixed window",
            "token_included": False,
            "global_duplicate_posts_removed": global_duplicates,
        },
        "communities": competitors,
        "posts": posts,
        "unavailable": unavailable,
        "benchmarks": {},
        "pagination": pagination,
        "api_audit": {
            "calls": client.calls,
            "retries": client.retries,
            "errors": client.errors,
        },
        "media_audit": {},
        "validation": {"completed": False},
    }
    write_json(run_dir / "raw" / "vk_posts_raw.json", dataset)
    write_json(run_dir / "raw" / "vk_comments_raw.json", comments)
    return dataset


def process_dataset(
    run_dir: Path,
    client: VkClient,
    allow_degraded: bool = False,
    degraded_consent: str | None = None,
    opener: Any = None,
    model_factory: Any = None,
) -> dict[str, Any]:
    dataset_path = run_dir / "raw" / "vk_posts_raw.json"
    dataset = load_json(dataset_path)
    posts = dataset["posts"]
    reusable_transcripts: dict[str, dict[str, Any]] = {}
    existing_dataset_path = run_dir / "outputs" / "vk_posts_dataset.json"
    if existing_dataset_path.exists():
        try:
            existing = load_json(existing_dataset_path)
            same_source = (
                existing.get("source", {}).get("competitor_set_sha256")
                == dataset.get("source", {}).get("competitor_set_sha256")
            )
            same_window = existing.get("window") == dataset.get("window")
            if same_source and same_window:
                for old_post in existing.get("posts") or []:
                    transcription = (old_post.get("media") or {}).get("transcription")
                    if isinstance(transcription, dict) and transcription.get(
                        "status"
                    ) in {"success", "no_speech"}:
                        reusable_transcripts[str(old_post["post_id"])] = transcription
        except Exception:
            reusable_transcripts = {}
    dataset["benchmarks"] = apply_benchmarks(posts)
    expected_images = downloaded_images = confirmed_clips = downloaded_clips = 0
    expected_collages = created_collages = 0
    media_paths: list[str] = []
    transcription_rows = []
    unavailable = dataset.setdefault("unavailable", [])

    def add_unavailable(
        post: dict[str, Any], object_name: str, reason: str, scope: str
    ) -> None:
        unavailable.append(
            {
                "object": object_name,
                "link": post["url"],
                "reason": reason,
                "scope": scope,
                "post_id": post["post_id"],
            }
        )
        post["data_status"] = "partial"

    for post in posts:
        if post["is_best"]:
            candidates = image_candidates(post)
            expected_images += len(candidates)
            if candidates:
                expected_collages += 1
            local_images = []
            for index, candidate in enumerate(candidates, 1):
                try:
                    cache, _ = cached_download(
                        candidate["url"],
                        run_dir / "cache" / "images",
                        opener=opener or urllib.request.urlopen,
                    )
                    target = (
                        run_dir
                        / "media"
                        / "images"
                        / f"{post['post_id'].replace('-', 'm')}_{index}.jpg"
                    )
                    width, height = normalize_image(cache, target)
                    downloaded_images += 1
                    local_images.append(target)
                    media_paths.append(target.relative_to(run_dir).as_posix())
                    post["media"]["images"].append(
                        {
                            "path": target.relative_to(run_dir).as_posix(),
                            "width": width,
                            "height": height,
                            "kind": candidate["kind"],
                        }
                    )
                except Exception as exc:
                    add_unavailable(
                        post,
                        f"Изображение {post['post_id']} №{index}",
                        f"Загрузка или нормализация не выполнена: {type(exc).__name__}",
                        "image",
                    )
            if local_images:
                collage = (
                    run_dir
                    / "media"
                    / "collages"
                    / f"{post['post_id'].replace('-', 'm')}.jpg"
                )
                try:
                    collage_width, collage_height = make_collage(local_images, collage)
                    created_collages += 1
                    media_paths.append(collage.relative_to(run_dir).as_posix())
                    post["media"]["collage_path"] = collage.relative_to(run_dir).as_posix()
                    post["media"]["collage_width"] = collage_width
                    post["media"]["collage_height"] = collage_height
                except Exception as exc:
                    add_unavailable(
                        post,
                        f"Коллаж {post['post_id']}",
                        f"Коллаж не создан: {type(exc).__name__}",
                        "collage",
                    )
        if post["post_type"] == "Клип":
            confirmed_clips += 1
            video = next(item for item in post["verified_videos"] if item.get("is_clip"))
            try:
                outcome = download_confirmed_clip(
                    client,
                    post["post_id"],
                    video,
                    run_dir,
                    opener=opener or urllib.request.urlopen,
                )
            except Exception as exc:
                outcome = {
                    "status": "unavailable",
                    "reason": f"clip download failed: {type(exc).__name__}",
                }
            if outcome["status"] == "downloaded":
                downloaded_clips += 1
                post["media"]["clip_path"] = outcome["path"]
                absolute = run_dir / outcome["path"]
                media_paths.append(absolute.relative_to(run_dir).as_posix())
                transcript = reusable_transcripts.get(str(post["post_id"]))
                if transcript is None:
                    transcript = transcribe_clip(
                        absolute,
                        run_dir / "cache" / "whisper",
                        model_factory=model_factory,
                    )
            else:
                add_unavailable(
                    post,
                    f"Клип {post['post_id']}",
                    str(outcome.get("reason") or "Клип недоступен"),
                    "clip",
                )
                transcript = {
                    "status": "unavailable",
                    "transcript": "Видео недоступно для локальной расшифровки.",
                    "error": outcome.get("reason"),
                }
            post["media"]["transcription_status"] = transcript["status"]
            post["media"]["transcript"] = transcript["transcript"]
            post["media"]["transcription"] = transcript
            transcription_rows.append(
                {
                    "post_id": post["post_id"],
                    "status": transcript["status"],
                    "language": transcript.get("language"),
                }
            )
            if transcript["status"] not in {"success", "no_speech"}:
                add_unavailable(
                    post,
                    f"Расшифровка {post['post_id']}",
                    "Whisper не вернул success/no_speech",
                    "transcription",
                )
    media_summary = {
        "expected_images": expected_images,
        "downloaded_images": downloaded_images,
        "expected_collages": expected_collages,
        "created_collages": created_collages,
        "confirmed_clips": confirmed_clips,
        "downloaded_clips": downloaded_clips,
        "clip_transcriptions": transcription_rows,
        "media_paths": media_paths,
    }
    gate = evaluate_media_gate(
        media_summary,
        allow_degraded=allow_degraded,
        degraded_consent=degraded_consent,
        media_root=run_dir,
    )
    dataset["media_audit"] = {**media_summary, "gate": gate}
    write_json(dataset_path, dataset)
    return dataset


def create_json_outputs(run_dir: Path) -> dict[str, Any]:
    dataset = load_json(run_dir / "raw" / "vk_posts_raw.json")
    comments = load_json(run_dir / "raw" / "vk_comments_raw.json")
    validate_schema(
        dataset,
        REFERENCE_DIR / "vk-posts-dataset.schema.json",
        "vk_posts_dataset",
    )
    validate_schema(
        comments,
        REFERENCE_DIR / "vk-comments.schema.json",
        "vk_comments",
    )
    output_dir = run_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "vk_posts_dataset.json", dataset)
    write_json(output_dir / "vk_comments.json", comments)
    media = dataset.get("media_audit") or {}
    anonymized = {
        "status": dataset["status"],
        "window_hours": 168,
        "post_count": len(dataset["posts"]),
        "best_post_count": sum(1 for item in dataset["posts"] if item.get("is_best")),
        "post_types": dict(Counter(item["post_type"] for item in dataset["posts"])),
        "images": {
            "expected": media.get("expected_images", 0),
            "downloaded": media.get("downloaded_images", 0),
            "collages": sum(
                1 for item in dataset["posts"] if item["media"].get("collage_path")
            ),
        },
        "clips": {
            "confirmed": media.get("confirmed_clips", 0),
            "downloaded": media.get("downloaded_clips", 0),
            "transcriptions": dict(
                Counter(
                    item["status"] for item in media.get("clip_transcriptions") or []
                )
            ),
        },
        "unavailable_count": len(dataset.get("unavailable") or []),
        "anonymization": {
            "community_names": False,
            "community_ids": False,
            "post_ids": False,
            "urls": False,
            "media_paths": False,
        },
    }
    validate_schema(
        anonymized,
        REFERENCE_DIR / "media-summary.schema.json",
        "media_summary",
    )
    write_json(output_dir / "media_summary_anonymized.json", anonymized)
    passport = {
        "status": dataset["status"],
        "run_started_at": dataset["run_started_at"],
        "window": dataset["window"],
        "source": dataset["source"],
        "api_audit": dataset["api_audit"],
        "pagination": dataset["pagination"],
        "media_gate": media.get("gate"),
        "hashes": {
            "vk_posts_dataset.json": sha256_file(output_dir / "vk_posts_dataset.json"),
            "vk_comments.json": sha256_file(output_dir / "vk_comments.json"),
            "media_summary_anonymized.json": sha256_file(
                output_dir / "media_summary_anonymized.json"
            ),
        },
    }
    write_json(output_dir / "run_passport.json", passport)
    return dataset


def validate_outputs(
    competitor_set_path: Path,
    run_dir: Path,
    literal_token: str | None = None,
) -> dict[str, Any]:
    output_dir = run_dir / "outputs"
    paths = {
        "dataset": output_dir / "vk_posts_dataset.json",
        "comments": output_dir / "vk_comments.json",
        "media": output_dir / "media_summary_anonymized.json",
        "passport": output_dir / "run_passport.json",
        "xlsx": output_dir / "vk_best_posts.xlsx",
        "workbook_validation": output_dir / "workbook_validation.json",
    }
    issues = [f"missing:{name}" for name, path in paths.items() if not path.is_file()]
    if issues:
        raise InputValidationError(";".join(issues))
    dataset = load_json(paths["dataset"])
    comments = load_json(paths["comments"])
    media_summary = load_json(paths["media"])
    passport = load_json(paths["passport"])
    source = load_json(competitor_set_path)
    schema_checks = (
        (
            source,
            REFERENCE_DIR / "competitor-set.schema.json",
            "competitor_set",
        ),
        (
            dataset,
            REFERENCE_DIR / "vk-posts-dataset.schema.json",
            "vk_posts_dataset",
        ),
        (
            comments,
            REFERENCE_DIR / "vk-comments.schema.json",
            "vk_comments",
        ),
        (
            media_summary,
            REFERENCE_DIR / "media-summary.schema.json",
            "media_summary",
        ),
        (
            passport,
            REFERENCE_DIR / "run-passport.schema.json",
            "run_passport",
        ),
    )
    for value, schema_path, label in schema_checks:
        try:
            validate_schema(value, schema_path, label)
        except InputValidationError as exc:
            issues.append(f"schema:{label}:{exc}")
    if issues:
        write_json(
            output_dir / "validation_report.json",
            {
                "passed": False,
                "status": dataset.get("status") if isinstance(dataset, dict) else None,
                "issues": issues,
                "counts": {},
                "secret_hits": [],
            },
        )
        raise InputValidationError(";".join(issues))
    validate_competitor_set(
        source, competitor_set_path.read_text(encoding="utf-8")
    )
    if dataset["source"]["competitor_set_sha256"] != sha256_file(competitor_set_path):
        issues.append("source_hash_mismatch")
    preflight_path = run_dir / "preflight.json"
    if not preflight_path.is_file():
        issues.append("missing_preflight")
    else:
        preflight = load_json(preflight_path)
        if preflight.get("competitor_set_sha256") != sha256_file(
            competitor_set_path
        ):
            issues.append("preflight_source_hash_mismatch")
        if preflight.get("started_at") != dataset.get("run_started_at"):
            issues.append("run_start_mismatch")
        if preflight.get("timezone") != "Europe/Moscow":
            issues.append("preflight_timezone_mismatch")
    if dataset.get("timezone") != "Europe/Moscow":
        issues.append("dataset_timezone_mismatch")
    if dataset.get("source", {}).get("new_competitor_search_performed") is not False:
        issues.append("competitor_search_detected")
    post_ids = [str(item["post_id"]) for item in dataset["posts"]]
    if len(post_ids) != len(set(post_ids)):
        issues.append("duplicate_post_ids")
    if set(post_ids) != set(comments):
        issues.append("comment_ids_mismatch")
    start_dt = parse_timestamp(dataset["window"]["start"])
    end_dt = parse_timestamp(dataset["window"]["end"])
    if (end_dt - start_dt).total_seconds() != 168 * 3600:
        issues.append("invalid_window_duration")
    grouped: dict[str, list[dict[str, Any]]] = {}
    allowed_post_types = {"Клип", "Видео", "Картинка", "Карусель", "Текстовый пост"}
    for post in dataset["posts"]:
        grouped.setdefault(str(post["community_id"]), []).append(post)
        post_time = parse_timestamp(post["date"])
        if post_time < start_dt or post_time > end_dt:
            issues.append(f"post_outside_window:{post['post_id']}")
        if post.get("post_type") not in allowed_post_types:
            issues.append(f"invalid_post_type:{post['post_id']}")
        for video in post.get("verified_videos") or []:
            status = video.get("verification_status")
            if status not in {"confirmed_clip", "confirmed_video"}:
                issues.append(f"unverified_video:{post['post_id']}")
            if status == "confirmed_clip" and not video.get("is_clip"):
                issues.append(f"clip_flag_mismatch:{post['post_id']}")
        if post.get("post_type") == "Видео" and not any(
            item.get("verification_status") == "confirmed_video"
            for item in post.get("verified_videos") or []
        ):
            issues.append(f"ordinary_video_unconfirmed:{post['post_id']}")
        expected = calculate_er(
            post["likes"], post["comments"], post["reposts"], post["views"]
        )
        if expected is None:
            if post.get("er") is not None:
                issues.append(f"er_mismatch:{post['post_id']}")
            if post.get("result") != "ER не рассчитан":
                issues.append(f"no_views_status_mismatch:{post['post_id']}")
        elif not math.isclose(float(post["er"]), expected, abs_tol=1e-12):
            issues.append(f"er_mismatch:{post['post_id']}")
        comment_entry = comments.get(str(post["post_id"])) or {}
        top = comment_entry.get("top_level_comments") or []
        collected_comments = len(top) + sum(
            len(item.get("replies") or []) for item in top
        )
        if collected_comments != int(post["comments"]):
            issues.append(f"comment_count_mismatch:{post['post_id']}")
        for image in post["media"].get("images") or []:
            if not (run_dir / image["path"]).is_file():
                issues.append(f"missing_media:{image['path']}")
        for key in ("collage_path", "clip_path"):
            value = post["media"].get(key)
            if value and not (run_dir / value).is_file():
                issues.append(f"missing_media:{value}")
    unavailable_seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(dataset.get("unavailable") or []):
        identity = (
            str(item.get("object") or ""),
            str(item.get("link") or ""),
            str(item.get("reason") or ""),
        )
        if not all(identity):
            issues.append(f"invalid_unavailable:{index}")
        if identity in unavailable_seen:
            issues.append(f"duplicate_unavailable:{index}")
        unavailable_seen.add(identity)
    for community_id, items in grouped.items():
        valid = [float(item["er"]) for item in items if item.get("er") is not None]
        expected_benchmark = sum(valid) / len(valid) if valid else None
        stored = (dataset.get("benchmarks") or {}).get(community_id, {}).get("benchmark")
        if expected_benchmark is None:
            if stored is not None:
                issues.append(f"benchmark_mismatch:{community_id}")
        elif stored is None or not math.isclose(
            float(stored), expected_benchmark, abs_tol=1e-12
        ):
            issues.append(f"benchmark_mismatch:{community_id}")
        for item in items:
            expected_best = bool(
                expected_benchmark is not None
                and item.get("er") is not None
                and float(item["er"]) >= expected_benchmark
            )
            if bool(item.get("is_best")) != expected_best:
                issues.append(f"best_flag_mismatch:{item['post_id']}")
    try:
        evaluate_media_gate(
            dataset.get("media_audit") or {},
            allow_degraded=bool(
                (dataset.get("media_audit") or {}).get("gate", {}).get("degraded")
            ),
            degraded_consent=(
                "recorded"
                if (dataset.get("media_audit") or {})
                .get("gate", {})
                .get("degraded_consent_recorded")
                else None
            ),
            media_root=run_dir,
        )
    except MediaGateError as exc:
        issues.append(f"media_gate:{exc}")
    passport_hash_targets = {
        "vk_posts_dataset.json": paths["dataset"],
        "vk_comments.json": paths["comments"],
        "media_summary_anonymized.json": paths["media"],
        "vk_best_posts.xlsx": paths["xlsx"],
        "workbook_validation.json": paths["workbook_validation"],
    }
    for hash_name, hash_path in passport_hash_targets.items():
        if passport.get("hashes", {}).get(hash_name) != sha256_file(hash_path):
            issues.append(f"passport_hash_mismatch:{hash_name}")
    workbook_validation = load_json(paths["workbook_validation"])
    if workbook_validation.get("sheets") != [
        "Сводка",
        "Лучшие посты",
        "Проверенные посты",
        "Недоступно",
    ]:
        issues.append("workbook_sheet_mismatch")
    try:
        deep_workbook_validation = inspect_xlsx(
            paths["xlsx"],
            dataset,
            EXPECTED_SHEETS,
            BEST_COLUMNS,
            CHECKED_COLUMNS,
            UNAVAILABLE_COLUMNS,
        )
        if workbook_validation.get("deep_validation") != deep_workbook_validation:
            issues.append("workbook_deep_validation_mismatch")
    except InputValidationError as exc:
        issues.append(f"xlsx_contract:{exc}")
    secret_hits = scan_secret_files(list(paths.values()), literal_secret=literal_token)
    if secret_hits:
        issues.append("secret_detected")
    if not issues:
        dataset["validation"] = {
            "completed": True,
            "validated_at": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "issues": [],
        }
        write_json(paths["dataset"], dataset)
        passport.setdefault("hashes", {})["vk_posts_dataset.json"] = sha256_file(
            paths["dataset"]
        )
        write_json(paths["passport"], passport)
    report = {
        "passed": not issues,
        "status": dataset.get("status"),
        "issues": issues,
        "counts": {
            "communities": len(dataset["communities"]),
            "posts": len(dataset["posts"]),
            "best_posts": sum(1 for item in dataset["posts"] if item.get("is_best")),
        },
        "secret_hits": secret_hits,
    }
    write_json(output_dir / "validation_report.json", report)
    transcription_counts: dict[str, int] = {}
    embedded_images = 0
    confirmed_clips = 0
    for post in dataset["posts"]:
        media = post.get("media") or {}
        embedded_images += 1 if media.get("collage_path") else 0
        if post.get("post_type") == "Клип":
            confirmed_clips += 1
            transcription_status = str(
                media.get("transcription_status") or "unavailable"
            )
            transcription_counts[transcription_status] = (
                transcription_counts.get(transcription_status, 0) + 1
            )
    final_validation = {
        "schema_version": "1.0",
        "passed": not issues,
        "issues": issues,
        "status": dataset.get("status"),
        "confirmed_at": dataset.get("confirmed_at"),
        "confirmation_date": dataset.get("confirmation_date"),
        "source": {
            "competitor_set_sha256": dataset["source"]["competitor_set_sha256"],
            "competitor_set_status": dataset["source"]["competitor_set_status"],
        },
        "counts": {
            "communities": len(dataset["communities"]),
            "posts": len(dataset["posts"]),
            "best_posts": sum(
                1 for item in dataset["posts"] if item.get("is_best")
            ),
            "comments_post_keys": len(comments),
            "embedded_images": embedded_images,
            "confirmed_clips": confirmed_clips,
            "transcriptions": transcription_counts,
        },
        "hashes": {
            "dataset": sha256_file(paths["dataset"]),
            "comments": sha256_file(paths["comments"]),
            "xlsx": sha256_file(paths["xlsx"]),
            "media_summary": sha256_file(paths["media"]),
            "workbook_validation": sha256_file(paths["workbook_validation"]),
            "run_passport": sha256_file(paths["passport"]),
        },
        "secret_scan": {"passed": not secret_hits, "hits": secret_hits},
    }
    write_json(output_dir / "final_validation.json", final_validation)
    validate_schema(
        final_validation,
        REFERENCE_DIR / "final-validation.schema.json",
        "final_validation",
    )
    if issues:
        raise InputValidationError(";".join(issues))
    return report


def confirm_outputs(run_dir: Path, confirmed_at: str) -> None:
    parse_timestamp(confirmed_at)
    output_dir = run_dir / "outputs"
    for name in (
        "vk_posts_dataset.json",
        "media_summary_anonymized.json",
        "run_passport.json",
    ):
        path = output_dir / name
        value = load_json(path)
        value["status"] = "confirmed"
        value["confirmed_at"] = confirmed_at
        value["confirmation_date"] = confirmed_at[:10]
        write_json(path, value)
    passport_path = output_dir / "run_passport.json"
    passport = load_json(passport_path)
    passport["hashes"]["vk_posts_dataset.json"] = sha256_file(
        output_dir / "vk_posts_dataset.json"
    )
    passport["hashes"]["media_summary_anonymized.json"] = sha256_file(
        output_dir / "media_summary_anonymized.json"
    )
    write_json(passport_path, passport)
