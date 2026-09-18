from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from vk_api import (
    VKAPIError,
    collect_wall_posts,
    dedupe_candidates,
    dedupe_posts,
    utc_iso_from_epoch,
)
from workflow import (
    COMPETITOR_LABELS,
    COMPETITOR_TYPES,
    REQUIRED_SOURCE_SUPPORT,
    normalize_context,
    source_origin,
    source_url_is_vk,
    validate_business_context_document,
    validate_review_document,
)
from xlsx_writer import (
    COMPETITOR_HEADERS,
    SHEET_NAMES,
    workbook_rows,
    workbook_sheet_names,
    write_workbook,
)


SECRET_PATTERNS = [
    re.compile(r"vk1\.[A-Za-z0-9._-]{20,}", re.IGNORECASE),
    re.compile(r"access_token\s*[=:]\s*[A-Za-z0-9._-]{12,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{20,}", re.IGNORECASE),
]
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "key",
    "secret",
    "sig",
    "signature",
    "token",
}
VALID_TYPES = set(COMPETITOR_TYPES)
JSON_NAME = "competitor_set.json"
AUDIT_NAME = "candidate_audit.json"
PASSPORT_NAME = "run_passport.json"
RAW_NAME = "raw_candidates.json"
MEASURED_NAME = "measured_candidates.json"
REVIEWS_NAME = "candidate_reviews.json"
XLSX_NAME = "competitor_analysis.xlsx"
HASH_NAME = "business_context.sha256"
SKILL_ROOT = Path(__file__).resolve().parents[1]
COMPETITOR_SCHEMA_PATH = (
    SKILL_ROOT / "references" / "competitor-set.schema.json"
)
REVIEW_SCHEMA_PATH = (
    SKILL_ROOT / "references" / "candidate-review.schema.json"
)


class PipelineError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{Path(path).name}: invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PipelineError(f"{Path(path).name}: root must be an object")
    return document


def canonical_json(document: Any) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def write_json(path: Path, document: Any) -> None:
    Path(path).write_text(canonical_json(document), encoding="utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def scan_text_for_secrets(text: str) -> list[str]:
    return [pattern.pattern for pattern in SECRET_PATTERNS if pattern.search(text)]


def scan_file_for_secrets(path: Path) -> list[str]:
    path = Path(path)
    payloads: list[tuple[str, bytes]] = []
    if path.suffix.lower() == ".xlsx":
        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith((".xml", ".rels")):
                        payloads.append((name, archive.read(name)))
        except zipfile.BadZipFile:
            return ["invalid_xlsx"]
    else:
        payloads.append((path.name, path.read_bytes()))
    hits: list[str] = []
    for name, payload in payloads:
        text = payload.decode("utf-8", errors="ignore")
        for pattern in scan_text_for_secrets(text):
            hits.append(f"{name}:{pattern}")
    return hits


def assert_no_secrets(document: Any, label: str) -> None:
    hits = scan_text_for_secrets(json.dumps(document, ensure_ascii=False))
    if hits:
        raise PipelineError(f"{label}: potential secret detected")


def sanitize_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"}:
        return None
    safe_query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in SENSITIVE_QUERY_KEYS
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment)
    )


def validate_business_context(context: dict[str, Any]) -> list[str]:
    errors = validate_business_context_document(context)
    security = context.get("security") or {}
    if isinstance(security, dict) and (
        security.get("secrets_included") is True
        or security.get("contains_secrets") is True
    ):
        errors.append("business context reports included secrets")
    try:
        assert_no_secrets(context, "business_context.json")
    except PipelineError as exc:
        errors.append(str(exc))
    return errors


def validate_candidate_reviews(reviews: dict[str, Any]) -> list[str]:
    errors = validate_review_document(reviews)
    try:
        assert_no_secrets(reviews, REVIEWS_NAME)
    except PipelineError as exc:
        errors.append(str(exc))
    return errors


def validate_competitor_set_schema(
    competitor_set: dict[str, Any],
) -> list[str]:
    """Validate the deterministic subset of the bundled JSON Schema."""
    errors: list[str] = []
    schema = load_json(COMPETITOR_SCHEMA_PATH)
    if competitor_set.get("schema_version") != "1.1":
        errors.append("competitor_set.json must use schema_version 1.1")
    required_top = set(schema.get("required", []))
    missing_top = required_top - set(competitor_set)
    if missing_top:
        errors.append(
            "competitor_set.json missing: " + ", ".join(sorted(missing_top))
        )
    allowed_top = set(schema.get("properties", {}))
    unexpected_top = set(competitor_set) - allowed_top
    if unexpected_top:
        errors.append(
            "competitor_set.json unexpected fields: "
            + ", ".join(sorted(unexpected_top))
        )
    if competitor_set.get("status") not in {"draft", "confirmed"}:
        errors.append("competitor_set.json has invalid status")
    definitions = schema.get("$defs", {})
    for key, definition_name in (
        ("provenance", "provenance"),
        ("window", "window"),
        ("criteria", "criteria"),
    ):
        value = competitor_set.get(key)
        if not isinstance(value, dict):
            errors.append(f"competitor_set.json {key} must be an object")
            continue
        definition = definitions[definition_name]
        missing = set(definition.get("required", [])) - set(value)
        if missing:
            errors.append(
                f"competitor_set.json {key} missing: "
                + ", ".join(sorted(missing))
            )
        if definition.get("additionalProperties") is False:
            unexpected = set(value) - set(definition.get("properties", {}))
            if unexpected:
                errors.append(
                    f"competitor_set.json {key} unexpected: "
                    + ", ".join(sorted(unexpected))
                )
    competitor_definition = definitions["competitor"]
    competitor_required = set(competitor_definition.get("required", []))
    competitor_allowed = set(competitor_definition.get("properties", {}))
    competitors = competitor_set.get("competitors")
    if not isinstance(competitors, list):
        errors.append("competitor_set.json competitors must be an array")
    else:
        for index, item in enumerate(competitors):
            label = f"competitor_set.json competitors[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label} must be an object")
                continue
            missing = competitor_required - set(item)
            if missing:
                errors.append(f"{label} missing: {', '.join(sorted(missing))}")
            unexpected = set(item) - competitor_allowed
            if unexpected:
                errors.append(
                    f"{label} unexpected: {', '.join(sorted(unexpected))}"
                )
    post_definition = definitions["post"]
    post_required = set(post_definition.get("required", []))
    post_allowed = set(post_definition.get("properties", {}))
    posts = competitor_set.get("posts_in_window")
    if not isinstance(posts, list):
        errors.append("competitor_set.json posts_in_window must be an array")
    else:
        for index, item in enumerate(posts):
            label = f"competitor_set.json posts_in_window[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label} must be an object")
                continue
            missing = post_required - set(item)
            if missing:
                errors.append(f"{label} missing: {', '.join(sorted(missing))}")
            unexpected = set(item) - post_allowed
            if unexpected:
                errors.append(
                    f"{label} unexpected: {', '.join(sorted(unexpected))}"
                )
    return errors


def search_config(context: dict[str, Any]) -> dict[str, Any]:
    errors = validate_business_context(context)
    if errors:
        raise PipelineError("; ".join(errors))
    config = normalize_context(context)
    return {
        **config,
        "window_hours": config["metric_window_hours"],
        "primary_city": config["primary_geography"],
    }


def normalize_city(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("title") or value.get("name")
    return str(value).strip() if value else None


def normalized_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    community_id = int(candidate.get("community_id") or candidate.get("id") or 0)
    screen_name = (
        candidate.get("screen_name")
        or candidate.get("domain")
        or f"club{community_id}"
    )
    wall = candidate.get("wall") if isinstance(candidate.get("wall"), dict) else {}
    posts = candidate.get("posts")
    if not isinstance(posts, list):
        posts = wall.get("posts", [])
    return {
        **candidate,
        "community_id": community_id,
        "screen_name": screen_name,
        "vk": f"https://vk.com/{screen_name}",
        "followers": candidate.get("followers", candidate.get("members_count")),
        "city_name": normalize_city(candidate.get("city")),
        "site": sanitize_url(candidate.get("site")),
        "posts": dedupe_posts(posts),
        "latest_own_publication_epoch": candidate.get(
            "latest_own_publication_epoch",
            wall.get("latest_own_publication_epoch"),
        ),
        "pages_fetched": wall.get(
            "pages_fetched",
            candidate.get("pages_fetched"),
        ),
        "pagination_stop_reason": wall.get(
            "stop_reason",
            candidate.get("pagination_stop_reason"),
        ),
    }


def counter_value(post: dict[str, Any], field: str) -> int:
    value = post.get(field, 0)
    if isinstance(value, dict):
        value = value.get("count", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def post_metrics(post: dict[str, Any]) -> dict[str, Any]:
    likes = counter_value(post, "likes")
    comments = counter_value(post, "comments")
    reposts = counter_value(post, "reposts")
    views_raw = post.get("views")
    if isinstance(views_raw, dict):
        views_raw = views_raw.get("count")
    try:
        views = int(views_raw) if views_raw is not None else None
    except (TypeError, ValueError):
        views = None
    interactions = likes + comments + reposts
    er = interactions / views if views and views > 0 else None
    if views is None:
        exclusion_reason = "missing_views"
    elif views == 0:
        exclusion_reason = "zero_views"
    elif views < 0:
        exclusion_reason = "invalid_views"
        er = None
    else:
        exclusion_reason = None
    return {
        "likes": likes,
        "comments": comments,
        "reposts": reposts,
        "views": views,
        "interactions": interactions,
        "er": er,
        "er_exclusion_reason": exclusion_reason,
    }


def mean_er(posts: Iterable[dict[str, Any]]) -> float | None:
    values = [post_metrics(post)["er"] for post in posts]
    measurable = [value for value in values if value is not None]
    return sum(measurable) / len(measurable) if measurable else None


def review_index(reviews: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in reviews.get("candidates", []):
        if not isinstance(item, dict):
            continue
        community_id = int(item.get("community_id", 0))
        if community_id > 0:
            result[community_id] = dict(item)
    return result


def follower_pass(value: Any, config: dict[str, Any]) -> bool:
    try:
        followers = int(value)
    except (TypeError, ValueError):
        return False
    if config["followers_inclusive"]:
        return config["followers_min"] <= followers <= config["followers_max"]
    return config["followers_min"] < followers < config["followers_max"]


def candidate_stage(
    candidate: dict[str, Any],
    review: dict[str, Any] | None,
    config: dict[str, Any],
    activity_start_epoch: int,
    window_end_epoch: int,
) -> tuple[str | None, str | None, str | None]:
    prefilter = candidate.get("prefilter")
    if isinstance(prefilter, dict) and prefilter.get("passed") is False:
        return (
            str(prefilter.get("stage") or "prefilter"),
            str(prefilter.get("code") or "prefilter_failed"),
            str(prefilter.get("reason") or "Кандидат не прошёл измеримый фильтр"),
        )
    if (
        candidate.get("deactivated")
        or int(candidate.get("is_closed") or 0) != 0
        or candidate["community_id"] <= 0
    ):
        return "vk_presence", "vk_unavailable", "VK-сообщество закрыто или недоступно"
    if (
        not candidate.get("required_inclusion")
        and not follower_pass(candidate.get("followers"), config)
    ):
        return (
            "followers",
            "followers_out_of_range",
            (
                f"Подписчики {candidate.get('followers')} не входят в диапазон "
                f"{config['followers_min']}–{config['followers_max']}"
            ),
        )
    if review is None:
        return (
            "semantic_review",
            "review_not_processed",
            "Кандидат не открывался после достижения целевого количества",
        )
    if review.get("decision") != "include":
        return (
            "semantic_review",
            str(review.get("exclusion_code") or "semantic_mismatch"),
            str(review.get("reason") or "Не соответствует смысловым критериям"),
        )
    if review.get("competitor_type") not in VALID_TYPES:
        return (
            "semantic_review",
            "invalid_classification",
            "Не назначен допустимый тип конкурента",
        )
    if review.get("geography_match") is not True:
        return (
            "geography",
            "geography_not_confirmed",
            "География не подтверждена первичными источниками",
        )
    source_checks = review.get("source_checks")
    if not isinstance(source_checks, list) or not source_checks:
        return (
            "sources",
            "sources_missing",
            "Не сохранены проверенные первичные источники",
        )
    wall = candidate.get("wall") if isinstance(candidate.get("wall"), dict) else {}
    if wall.get("stop_reason") == "vk_api_error":
        return (
            "wall_collection",
            "wall_api_error",
            "Не удалось получить публикации VK после повторов",
        )
    latest_epoch = candidate.get("latest_own_publication_epoch") or wall.get(
        "latest_own_publication_epoch"
    )
    if not isinstance(latest_epoch, int) or latest_epoch < activity_start_epoch:
        return (
            "freshness",
            "no_recent_own_publication",
            (
                "Последняя собственная публикация старше критерия свежести "
                "из карточки бизнеса"
            ),
        )
    return None, None, None


def output_post(
    post: dict[str, Any],
    candidate: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    metrics = post_metrics(post)
    community_id = candidate["community_id"]
    post_id = int(post.get("id") or post.get("post_id"))
    competitor_type_code = review["competitor_type"]
    return {
        "community_id": community_id,
        "owner_id": -community_id,
        "post_id": post_id,
        "published_at": utc_iso_from_epoch(int(post.get("date", 0))),
        **metrics,
        "is_pinned": bool(post.get("is_pinned")),
        "has_copy_history": bool(post.get("copy_history")),
        "text": str(post.get("text") or ""),
        "url": f"https://vk.com/wall-{community_id}_{post_id}",
        "community_name": str(candidate.get("name") or ""),
        "competitor_type": COMPETITOR_LABELS[competitor_type_code],
        "competitor_type_code": competitor_type_code,
    }


def build_outputs(
    context: dict[str, Any],
    raw: dict[str, Any],
    measured: dict[str, Any],
    reviews: dict[str, Any],
    *,
    context_hash: str,
    raw_hash: str,
    measured_hash: str,
    reviews_hash: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = search_config(context)
    metric_window = measured.get("metric_window")
    if not isinstance(metric_window, dict):
        raise PipelineError("measured_candidates.json: metric_window is required")
    window_start = parse_iso(metric_window["start"])
    window_end = parse_iso(metric_window["end"])
    if int((window_end - window_start).total_seconds() // 3600) != 31 * 24:
        raise PipelineError("metric window must be exactly 31×24 hours")
    window_start_epoch = int(window_start.timestamp())
    window_end_epoch = int(window_end.timestamp())
    activity_start_epoch = int(
        (
            window_end
            - timedelta(hours=config["activity_freshness_hours"])
        ).timestamp()
    )
    raw_provenance = raw.get("provenance", {})
    measured_provenance = measured.get("provenance", {})
    if raw_provenance.get("business_context_sha256") != context_hash:
        raise PipelineError("raw_candidates.json has broken business-context lineage")
    if measured_provenance.get("business_context_sha256") != context_hash:
        raise PipelineError(
            "measured_candidates.json has broken business-context lineage"
        )
    if measured_provenance.get("raw_candidates_sha256") != raw_hash:
        raise PipelineError("measured_candidates.json has broken raw-candidate lineage")
    if measured_provenance.get("candidate_reviews_sha256") != reviews_hash:
        raise PipelineError("measured_candidates.json has broken review lineage")

    candidates = dedupe_candidates(measured.get("candidates", []))
    candidates = [normalized_candidate(item) for item in candidates]
    candidates_by_id = {item["community_id"]: item for item in candidates}
    review_errors = validate_review_document(
        reviews,
        candidates_by_id=candidates_by_id,
        config=config,
    )
    if review_errors:
        raise PipelineError("; ".join(review_errors))
    reviews_by_id = review_index(reviews)
    audit_rows: list[dict[str, Any]] = []
    qualified: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unreviewed_eligible: list[int] = []

    for candidate in candidates:
        community_id = candidate["community_id"]
        review = reviews_by_id.get(community_id)
        stage, code, reason = candidate_stage(
            candidate,
            review,
            config,
            activity_start_epoch,
            window_end_epoch,
        )
        if (
            review is None
            and isinstance(candidate.get("prefilter"), dict)
            and candidate["prefilter"].get("passed") is True
        ):
            unreviewed_eligible.append(community_id)
        own_posts = [
            post
            for post in candidate.get("posts", [])
            if window_start_epoch
            <= int(post.get("date", 0))
            <= window_end_epoch
            and int(post.get("from_id", -community_id)) == -community_id
        ]
        own_posts = dedupe_posts(own_posts)
        audit_row = {
            "community_id": community_id,
            "name": candidate.get("name"),
            "vk": candidate["vk"],
            "site": sanitize_url(
                (review or {}).get("site") or candidate.get("site")
            ),
            "followers": candidate.get("followers"),
            "geography": (review or {}).get("geography")
            or candidate.get("city_name"),
            "last_own_publication_at": utc_iso_from_epoch(
                candidate.get("latest_own_publication_epoch")
                or max(
                    (int(post.get("date", 0)) for post in own_posts),
                    default=0,
                )
                or None
            ),
            "posts_in_window_count": len(own_posts),
            "average_er": mean_er(own_posts),
            "matched_queries": sorted(set(candidate.get("matched_queries", []))),
            "rank_score": candidate.get("rank_score"),
            "known_competitor": bool(candidate.get("known_competitor")),
            "required_inclusion": bool(candidate.get("required_inclusion")),
            "decision": "excluded" if stage else "qualified",
            "competitor_type": (review or {}).get("competitor_type"),
            "verified_sources": [
                source.get("url")
                for source in (review or {}).get("source_checks", [])
                if isinstance(source, dict) and source.get("url")
            ],
            "exclusion_stage": stage,
            "exclusion_code": code,
            "exclusion_reason": reason,
        }
        audit_rows.append(audit_row)
        if stage is None and review is not None:
            qualified.append((candidate, review))

    if len(qualified) < config["target_count"] and unreviewed_eligible:
        raise PipelineError(
            "more semantic review batches are required before declaring a shortfall"
        )

    qualified.sort(
        key=lambda pair: (
            not bool(pair[0].get("required_inclusion")),
            -float(pair[1].get("priority_score", 0)),
            -float(pair[0].get("rank_score") or 0),
            -int(pair[0].get("followers") or 0),
            pair[0]["community_id"],
        )
    )
    selected_pairs = qualified[: config["target_count"]]
    selected_ids = {candidate["community_id"] for candidate, _ in selected_pairs}
    for row in audit_rows:
        if row["community_id"] in selected_ids:
            row["decision"] = "included"
        elif row["decision"] == "qualified":
            row.update(
                {
                    "decision": "excluded",
                    "exclusion_stage": "target_selection",
                    "exclusion_code": "qualified_reserve",
                    "exclusion_reason": (
                        "Кандидат прошёл проверки, но остался за пределами "
                        "заданного количества по стабильному рейтингу"
                    ),
                }
            )

    competitors: list[dict[str, Any]] = []
    posts_out: list[dict[str, Any]] = []
    er_exclusions: list[dict[str, Any]] = []
    for candidate, review in selected_pairs:
        community_id = candidate["community_id"]
        candidate_posts = candidate.get("posts") or candidate.get("wall", {}).get(
            "posts", []
        )
        own_posts = dedupe_posts(
            post
            for post in candidate_posts
            if window_start_epoch
            <= int(post.get("date", 0))
            <= window_end_epoch
            and int(post.get("from_id", -community_id)) == -community_id
        )
        community_posts = [
            output_post(post, candidate, review) for post in own_posts
        ]
        posts_out.extend(community_posts)
        er_exclusions.extend(
            {
                "community_id": item["community_id"],
                "owner_id": item["owner_id"],
                "post_id": item["post_id"],
                "reason": item["er_exclusion_reason"],
            }
            for item in community_posts
            if item["er_exclusion_reason"] is not None
        )
        measurable = [item["er"] for item in community_posts if item["er"] is not None]
        all_own_posts = dedupe_posts(
            post
            for post in candidate_posts
            if int(post.get("from_id", -community_id)) == -community_id
        )
        latest_post = max(
            all_own_posts,
            key=lambda item: int(item.get("date", 0)),
            default={},
        )
        latest_epoch = int(latest_post.get("date", 0))
        source_checks = []
        for source in review.get("source_checks", []):
            if not isinstance(source, dict):
                continue
            safe_url = sanitize_url(source.get("url"))
            if not safe_url:
                continue
            source_checks.append(
                {
                    "url": safe_url,
                    "kind": source.get("kind"),
                    "official": True,
                    "checked_at": source.get("checked_at"),
                    "supports": sorted(set(source.get("supports", []))),
                }
            )
        sources = list(
            dict.fromkeys(source["url"] for source in source_checks)
        )
        competitor_type_code = review["competitor_type"]
        competitors.append(
            {
                "community_id": community_id,
                "name": str(candidate.get("name") or ""),
                "vk": candidate["vk"],
                "site": sanitize_url(review.get("site")),
                "competitor_type": COMPETITOR_LABELS[competitor_type_code],
                "competitor_type_code": competitor_type_code,
                "followers": int(candidate.get("followers") or 0),
                "last_own_publication_at": utc_iso_from_epoch(latest_epoch),
                "last_own_publication_url": (
                    f"https://vk.com/wall-{community_id}_"
                    f"{latest_post.get('id')}"
                ),
                "posts_in_window_count": len(community_posts),
                "measurable_er_post_count": len(measurable),
                "er_excluded_post_count": len(community_posts) - len(measurable),
                "average_er": (
                    sum(measurable) / len(measurable) if measurable else None
                ),
                "assortment": str(review.get("assortment") or ""),
                "audience": str(review.get("audience") or ""),
                "geography": str(
                    review.get("geography")
                    or candidate.get("city_name")
                    or ""
                ),
                "price_segment": str(
                    review.get("price_segment") or "not_established"
                ),
                "sales_model": str(review.get("sales_model") or ""),
                "production": str(review.get("production") or ""),
                "online_offline": str(review.get("online_offline") or ""),
                "why_competitor": str(review.get("reason") or ""),
                "key_feature_check": review.get("key_feature_check") or {},
                "classification_evidence": (
                    review.get("classification_evidence") or {}
                ),
                "required_inclusion": bool(candidate.get("required_inclusion")),
                "rank_score": candidate.get("rank_score"),
                "verified_sources": sources,
                "source_checks": source_checks,
            }
        )

    competitors.sort(key=lambda item: item["community_id"])
    posts_out.sort(
        key=lambda item: (
            item["community_id"],
            -int(parse_iso(item["published_at"]).timestamp()),
            item["post_id"],
        )
    )
    target_met = len(competitors) == config["target_count"]
    generated_at = utc_now_iso()
    provenance = {
        "business_context_file": "business_context.json",
        "business_context_sha256": context_hash,
        "raw_candidates_file": RAW_NAME,
        "raw_candidates_sha256": raw_hash,
        "measured_candidates_file": MEASURED_NAME,
        "measured_candidates_sha256": measured_hash,
        "candidate_reviews_file": REVIEWS_NAME,
        "candidate_reviews_sha256": reviews_hash,
    }
    competitor_set = {
        "schema_version": "1.1",
        "status": "draft",
        "generated_at": generated_at,
        "confirmation_date": None,
        "provenance": provenance,
        "window": {
            "timezone": "UTC",
            "start": metric_window["start"],
            "end": metric_window["end"],
            "duration_hours": 31 * 24,
            "inclusive": True,
        },
        "criteria": {
            "target_count": config["target_count"],
            "actual_count": len(competitors),
            "followers_min": config["followers_min"],
            "followers_max": config["followers_max"],
            "followers_inclusive": config["followers_inclusive"],
            "primary_city": config["primary_city"],
            "geography_rule": config["geography_rule"],
            "activity_freshness_hours": config["activity_freshness_hours"],
            "metric_window_hours": 31 * 24,
            "required_inclusions": config["required_inclusions"],
            "exclusions": config["exclusions"],
            "key_features": config["key_features"],
            "matching_policy": config["matching_policy"],
            "vk_required": True,
        },
        "classification_summary": {
            kind: sum(
                item["competitor_type_code"] == kind for item in competitors
            )
            for kind in ("direct", "indirect", "attention")
        },
        "competitors": competitors,
        "posts_in_window": posts_out,
        "er_exclusions": er_exclusions,
        "validation": {
            "target_met": target_met,
            "conditions_relaxed": False,
            "all_have_vk": all(item["vk"] for item in competitors),
            "secret_included": False,
        },
    }
    audit = {
        "schema_version": "1.1",
        "status": "draft",
        "generated_at": generated_at,
        "confirmation_date": None,
        "provenance": provenance,
        "counts": {
            "total": len(audit_rows),
            "included": sum(row["decision"] == "included" for row in audit_rows),
            "excluded": sum(row["decision"] == "excluded" for row in audit_rows),
        },
        "candidates": sorted(audit_rows, key=lambda item: item["community_id"]),
    }
    passport = {
        "schema_version": "1.1",
        "status": "draft",
        "generated_at": generated_at,
        "confirmation_date": None,
        "provenance": provenance,
        "vk_api_version": raw.get("vk_api_version"),
        "vk_methods": list(
            dict.fromkeys(
                [
                    *raw.get("vk_methods", []),
                    *measured.get("vk_methods", []),
                ]
            )
        ),
        "window": competitor_set["window"],
        "activity_window": measured.get("activity_window"),
        "search": raw.get("search", {}),
        "semantic_batches": raw.get("semantic_batches", {}),
        "pagination": measured.get("pagination", {}),
        "retry_policy": measured.get(
            "retry_policy",
            raw.get("retry_policy", {}),
        ),
        "known_and_required_resolution": raw.get(
            "known_and_required_resolution",
            [],
        ),
        "counts": {
            "candidates_discovered": len(candidates),
            "candidates_included": len(competitors),
            "candidates_excluded": audit["counts"]["excluded"],
            "posts_included": len(posts_out),
            "er_excluded_posts": len(er_exclusions),
            "vk_api_calls": int(raw.get("vk_api_calls") or 0)
            + int(measured.get("vk_api_calls") or 0),
        },
        "errors": [*raw.get("errors", []), *measured.get("errors", [])],
        "limitations": [
            *raw.get("limitations", []),
            *measured.get("limitations", []),
        ],
        "output_hashes": {},
        "security": {"secret_included": False},
    }
    assert_no_secrets(competitor_set, JSON_NAME)
    assert_no_secrets(audit, AUDIT_NAME)
    assert_no_secrets(passport, PASSPORT_NAME)
    return competitor_set, audit, passport


def build_measured_candidates(
    context: dict[str, Any],
    raw: dict[str, Any],
    reviews: dict[str, Any],
    *,
    context_hash: str,
    raw_hash: str,
    reviews_hash: str,
    call: Any,
    window_end: datetime,
    api_version: str,
    retry_policy: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = search_config(context)
    candidates = [
        normalized_candidate(item)
        for item in dedupe_candidates(raw.get("candidates", []))
    ]
    candidates_by_id = {item["community_id"]: item for item in candidates}
    review_errors = validate_review_document(
        reviews,
        candidates_by_id=candidates_by_id,
        config=config,
    )
    if review_errors:
        raise PipelineError("; ".join(review_errors))
    reviews_by_id = review_index(reviews)
    window_end = window_end.astimezone(timezone.utc)
    metric_start = window_end - timedelta(hours=31 * 24)
    activity_start = window_end - timedelta(
        hours=config["activity_freshness_hours"]
    )
    collection_start = min(metric_start, activity_start)

    previous_walls: dict[int, dict[str, Any]] = {}
    if previous is not None:
        provenance = previous.get("provenance", {})
        previous_window = previous.get("metric_window", {})
        if (
            provenance.get("business_context_sha256") != context_hash
            or provenance.get("raw_candidates_sha256") != raw_hash
            or previous_window.get("end")
            != window_end.isoformat().replace("+00:00", "Z")
        ):
            raise PipelineError("previous measured file belongs to another input/window")
        for item in previous.get("candidates", []):
            community_id = int(item.get("id") or item.get("community_id") or 0)
            wall = item.get("wall")
            if community_id > 0 and isinstance(wall, dict):
                if wall.get("stop_reason") not in {
                    "semantic_not_included",
                    "review_not_processed",
                }:
                    previous_walls[community_id] = wall

    errors: list[dict[str, Any]] = []
    pagination_rows: list[dict[str, Any]] = []
    measured_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        community_id = candidate["community_id"]
        review = reviews_by_id.get(community_id)
        prefilter = candidate.get("prefilter") or {}
        if prefilter.get("passed") is False:
            wall = {
                "community_id": community_id,
                "posts": [],
                "pages_fetched": 0,
                "stop_reason": "prefilter_excluded",
                "latest_own_publication_epoch": None,
            }
        elif review is None:
            wall = {
                "community_id": community_id,
                "posts": [],
                "pages_fetched": 0,
                "stop_reason": "review_not_processed",
                "latest_own_publication_epoch": None,
            }
        elif review.get("decision") != "include":
            wall = {
                "community_id": community_id,
                "posts": [],
                "pages_fetched": 0,
                "stop_reason": "semantic_not_included",
                "latest_own_publication_epoch": None,
            }
        elif community_id in previous_walls:
            wall = previous_walls[community_id]
        else:
            try:
                wall = collect_wall_posts(
                    call,
                    community_id,
                    window_start_epoch=int(collection_start.timestamp()),
                    window_end_epoch=int(window_end.timestamp()),
                    page_size=100,
                )
            except VKAPIError as exc:
                errors.append(
                    {
                        "community_id": community_id,
                        "method": "wall.get",
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
                wall = {
                    "community_id": community_id,
                    "posts": [],
                    "pages_fetched": 0,
                    "stop_reason": "vk_api_error",
                    "latest_own_publication_epoch": None,
                }
        item = dict(candidate)
        item["wall"] = wall
        measured_candidates.append(item)
        pagination_rows.append(
            {
                "community_id": community_id,
                "pages_fetched": wall.get("pages_fetched", 0),
                "stop_reason": wall.get("stop_reason"),
                "reused": community_id in previous_walls,
            }
        )

    return {
        "schema_version": "1.1",
        "generated_at": utc_now_iso(),
        "provenance": {
            "business_context_file": "business_context.json",
            "business_context_sha256": context_hash,
            "raw_candidates_file": RAW_NAME,
            "raw_candidates_sha256": raw_hash,
            "candidate_reviews_file": REVIEWS_NAME,
            "candidate_reviews_sha256": reviews_hash,
        },
        "metric_window": {
            "timezone": "UTC",
            "start": metric_start.isoformat().replace("+00:00", "Z"),
            "end": window_end.isoformat().replace("+00:00", "Z"),
            "duration_hours": 31 * 24,
            "inclusive": True,
        },
        "activity_window": {
            "timezone": "UTC",
            "start": activity_start.isoformat().replace("+00:00", "Z"),
            "end": window_end.isoformat().replace("+00:00", "Z"),
            "duration_hours": config["activity_freshness_hours"],
            "inclusive": True,
        },
        "collection_window": {
            "timezone": "UTC",
            "start": collection_start.isoformat().replace("+00:00", "Z"),
            "end": window_end.isoformat().replace("+00:00", "Z"),
            "inclusive": True,
        },
        "vk_api_version": api_version,
        "vk_methods": ["wall.get"],
        "pagination": {
            "wall_page_size": 100,
            "wall_stop_rule": (
                "stop at wall end or when every ordinary own post on the last "
                "full page is older than the collection window; pinned posts "
                "never trigger the stop"
            ),
            "rows": pagination_rows,
        },
        "retry_policy": retry_policy,
        "vk_api_calls": 0,
        "errors": errors,
        "limitations": [],
        "candidates": measured_candidates,
    }


def finalize_outputs(
    business_context_path: Path,
    raw_candidates_path: Path,
    measured_candidates_path: Path,
    candidate_reviews_path: Path,
    output_dir: Path,
) -> bool:
    business_context_path = Path(business_context_path)
    raw_candidates_path = Path(raw_candidates_path)
    measured_candidates_path = Path(measured_candidates_path)
    candidate_reviews_path = Path(candidate_reviews_path)
    output_dir = Path(output_dir)
    context = load_json(business_context_path)
    raw = load_json(raw_candidates_path)
    measured = load_json(measured_candidates_path)
    reviews = load_json(candidate_reviews_path)
    errors = validate_business_context(context)
    if errors:
        raise PipelineError("; ".join(errors))
    assert_no_secrets(raw, RAW_NAME)
    assert_no_secrets(measured, MEASURED_NAME)
    context_hash = sha256_file(business_context_path)
    raw_hash = sha256_file(raw_candidates_path)
    measured_hash = sha256_file(measured_candidates_path)
    reviews_hash = sha256_file(candidate_reviews_path)
    competitor_set, audit, passport = build_outputs(
        context,
        raw,
        measured,
        reviews,
        context_hash=context_hash,
        raw_hash=raw_hash,
        measured_hash=measured_hash,
        reviews_hash=reviews_hash,
    )
    schema_errors = validate_competitor_set_schema(competitor_set)
    if schema_errors:
        raise PipelineError("; ".join(schema_errors))
    output_dir.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(raw_candidates_path, output_dir / RAW_NAME)
    shutil.copyfile(measured_candidates_path, output_dir / MEASURED_NAME)
    shutil.copyfile(candidate_reviews_path, output_dir / REVIEWS_NAME)
    (output_dir / HASH_NAME).write_text(
        f"{context_hash}  business_context.json\n",
        encoding="utf-8",
    )
    write_json(output_dir / JSON_NAME, competitor_set)
    competitor_set_hash = sha256_file(output_dir / JSON_NAME)
    audit["provenance"]["competitor_set_sha256"] = competitor_set_hash
    passport["provenance"]["competitor_set_sha256"] = competitor_set_hash
    write_json(output_dir / AUDIT_NAME, audit)
    write_json(output_dir / PASSPORT_NAME, passport)
    write_workbook(output_dir / XLSX_NAME, competitor_set, audit, passport)
    passport["output_hashes"] = {
        JSON_NAME: competitor_set_hash,
        AUDIT_NAME: sha256_file(output_dir / AUDIT_NAME),
        XLSX_NAME: sha256_file(output_dir / XLSX_NAME),
    }
    write_json(output_dir / PASSPORT_NAME, passport)
    return bool(competitor_set["validation"]["target_met"])


def confirm_outputs(
    directory: Path,
    confirmation_date: str,
) -> None:
    directory = Path(directory)
    datetime.strptime(confirmation_date, "%Y-%m-%d")
    competitor_set = load_json(directory / JSON_NAME)
    audit = load_json(directory / AUDIT_NAME)
    passport = load_json(directory / PASSPORT_NAME)
    for document in (competitor_set, audit, passport):
        document["status"] = "confirmed"
        document["confirmation_date"] = confirmation_date
        document["confirmed_at"] = utc_now_iso()
    confirmed_at = competitor_set["confirmed_at"]
    audit["confirmed_at"] = confirmed_at
    passport["confirmed_at"] = confirmed_at
    write_json(directory / JSON_NAME, competitor_set)
    competitor_set_hash = sha256_file(directory / JSON_NAME)
    audit["provenance"]["competitor_set_sha256"] = competitor_set_hash
    passport["provenance"]["competitor_set_sha256"] = competitor_set_hash
    write_json(directory / AUDIT_NAME, audit)
    write_json(directory / PASSPORT_NAME, passport)
    write_workbook(directory / XLSX_NAME, competitor_set, audit, passport)
    passport["output_hashes"] = {
        JSON_NAME: competitor_set_hash,
        AUDIT_NAME: sha256_file(directory / AUDIT_NAME),
        XLSX_NAME: sha256_file(directory / XLSX_NAME),
    }
    write_json(directory / PASSPORT_NAME, passport)


def validate_output_directory(
    directory: Path,
    business_context_path: Path | None = None,
) -> list[str]:
    directory = Path(directory)
    errors: list[str] = []
    documents: dict[str, dict[str, Any]] = {}
    for name in (
        JSON_NAME,
        AUDIT_NAME,
        PASSPORT_NAME,
        RAW_NAME,
        MEASURED_NAME,
        REVIEWS_NAME,
    ):
        path = directory / name
        if not path.is_file():
            errors.append(f"missing {name}")
            continue
        try:
            documents[name] = load_json(path)
        except PipelineError as exc:
            errors.append(str(exc))
    if errors:
        return errors
    competitor_set = documents[JSON_NAME]
    audit = documents[AUDIT_NAME]
    passport = documents[PASSPORT_NAME]
    raw = documents[RAW_NAME]
    measured = documents[MEASURED_NAME]
    reviews = documents[REVIEWS_NAME]
    errors.extend(validate_competitor_set_schema(competitor_set))
    errors.extend(validate_candidate_reviews(reviews))

    status_values = {
        competitor_set.get("status"),
        audit.get("status"),
        passport.get("status"),
    }
    if len(status_values) != 1 or next(iter(status_values)) not in {
        "draft",
        "confirmed",
    }:
        errors.append("statuses are not synchronized")
    if competitor_set.get("status") == "confirmed":
        dates = {
            competitor_set.get("confirmation_date"),
            audit.get("confirmation_date"),
            passport.get("confirmation_date"),
        }
        if len(dates) != 1 or None in dates:
            errors.append("confirmation dates are not synchronized")
    provenance = competitor_set.get("provenance", {})
    if sha256_file(directory / RAW_NAME) != provenance.get("raw_candidates_sha256"):
        errors.append("broken provenance: raw_candidates.json hash mismatch")
    if sha256_file(directory / REVIEWS_NAME) != provenance.get(
        "candidate_reviews_sha256"
    ):
        errors.append("broken provenance: candidate_reviews.json hash mismatch")
    if sha256_file(directory / MEASURED_NAME) != provenance.get(
        "measured_candidates_sha256"
    ):
        errors.append("broken provenance: measured_candidates.json hash mismatch")
    if raw.get("provenance", {}).get("business_context_sha256") != provenance.get(
        "business_context_sha256"
    ):
        errors.append("broken provenance: raw context hash mismatch")
    if measured.get("provenance", {}).get(
        "business_context_sha256"
    ) != provenance.get("business_context_sha256"):
        errors.append("broken provenance: measured context hash mismatch")
    if measured.get("provenance", {}).get(
        "raw_candidates_sha256"
    ) != provenance.get("raw_candidates_sha256"):
        errors.append("broken provenance: measured raw-candidate hash mismatch")
    if measured.get("provenance", {}).get(
        "candidate_reviews_sha256"
    ) != provenance.get("candidate_reviews_sha256"):
        errors.append("broken provenance: measured review hash mismatch")
    if business_context_path is not None:
        if not Path(business_context_path).is_file():
            errors.append("business_context.json is missing")
        elif sha256_file(Path(business_context_path)) != provenance.get(
            "business_context_sha256"
        ):
            errors.append("broken provenance: business_context.json hash mismatch")
        else:
            try:
                context_errors = validate_business_context(
                    load_json(Path(business_context_path))
                )
                errors.extend(context_errors)
            except PipelineError as exc:
                errors.append(str(exc))
    competitor_set_hash = sha256_file(directory / JSON_NAME)
    if audit.get("provenance", {}).get(
        "competitor_set_sha256"
    ) != competitor_set_hash:
        errors.append("broken provenance: audit competitor_set hash mismatch")
    if passport.get("provenance", {}).get(
        "competitor_set_sha256"
    ) != competitor_set_hash:
        errors.append("broken provenance: passport competitor_set hash mismatch")

    competitors = competitor_set.get("competitors")
    posts = competitor_set.get("posts_in_window")
    if not isinstance(competitors, list) or not isinstance(posts, list):
        errors.append("competitor_set arrays are missing")
        return errors
    competitor_ids = [item.get("community_id") for item in competitors]
    if len(competitor_ids) != len(set(competitor_ids)):
        errors.append("duplicate competitor IDs")
    if not all(
        item.get("vk")
        and item.get("competitor_type_code") in VALID_TYPES
        and item.get("competitor_type")
        == COMPETITOR_LABELS[item["competitor_type_code"]]
        for item in competitors
    ):
        errors.append("invalid competitor identity or classification")
    for item in competitors:
        sources = item.get("verified_sources")
        checks = item.get("source_checks")
        if not isinstance(sources, list) or not isinstance(checks, list):
            errors.append("competitor sources are missing")
            break
        official_vk = [
            check
            for check in checks
            if isinstance(check, dict)
            and check.get("kind") == "official_vk"
            and check.get("official") is True
            and source_url_is_vk(check.get("url"))
        ]
        if not official_vk or not any(source_url_is_vk(url) for url in sources):
            errors.append("competitor lacks an official VK source")
            break
        site = item.get("site")
        if site and not any(
            isinstance(check, dict)
            and check.get("kind") == "official_website"
            and check.get("official") is True
            and source_origin(check.get("url")) == source_origin(site)
            for check in checks
        ):
            errors.append("competitor site is not verified")
            break
    post_keys = [(item.get("owner_id"), item.get("post_id")) for item in posts]
    if len(post_keys) != len(set(post_keys)):
        errors.append("duplicate post IDs")
    window = competitor_set.get("window", {})
    try:
        start = parse_iso(window["start"])
        end = parse_iso(window["end"])
    except (KeyError, TypeError, ValueError):
        errors.append("invalid result window")
        start = end = None
    grouped: dict[int, list[float]] = defaultdict(list)
    post_counts: Counter[int] = Counter()
    expected_er_exclusions: list[dict[str, Any]] = []
    for post in posts:
        community_id = post.get("community_id")
        if post.get("owner_id") != -community_id:
            errors.append("non-owner post included")
            break
        if start and end:
            published = parse_iso(post["published_at"])
            if not start <= published <= end:
                errors.append("post outside result window")
                break
        expected_interactions = (
            int(post.get("likes") or 0)
            + int(post.get("comments") or 0)
            + int(post.get("reposts") or 0)
        )
        if post.get("interactions") != expected_interactions:
            errors.append("post interaction mismatch")
            break
        views = post.get("views")
        expected_er = expected_interactions / views if views and views > 0 else None
        expected_reason = (
            None
            if views is not None and views > 0
            else "missing_views"
            if views is None
            else "zero_views"
            if views == 0
            else "invalid_views"
        )
        if post.get("er_exclusion_reason") != expected_reason:
            errors.append("post ER exclusion reason mismatch")
            break
        if expected_reason is not None:
            expected_er_exclusions.append(
                {
                    "community_id": community_id,
                    "owner_id": post.get("owner_id"),
                    "post_id": post.get("post_id"),
                    "reason": expected_reason,
                }
            )
        actual_er = post.get("er")
        if expected_er is None:
            if actual_er is not None:
                errors.append("zero-view ER must be null")
                break
        elif actual_er is None or not math.isclose(
            actual_er, expected_er, rel_tol=0, abs_tol=1e-12
        ):
            errors.append("post ER mismatch")
            break
        post_counts[community_id] += 1
        if actual_er is not None:
            grouped[community_id].append(actual_er)
    if competitor_set.get("er_exclusions") != expected_er_exclusions:
        errors.append("ER exclusion audit mismatch")
    for item in competitors:
        community_id = item["community_id"]
        values = grouped[community_id]
        expected = sum(values) / len(values) if values else None
        if item.get("posts_in_window_count") != post_counts[community_id]:
            errors.append(f"community {community_id}: post count mismatch")
        if item.get("measurable_er_post_count") != len(values):
            errors.append(f"community {community_id}: measurable ER count mismatch")
        if item.get("er_excluded_post_count") != (
            post_counts[community_id] - len(values)
        ):
            errors.append(f"community {community_id}: excluded ER count mismatch")
        actual = item.get("average_er")
        if expected is None:
            if actual is not None:
                errors.append(f"community {community_id}: average ER must be null")
        elif actual is None or not math.isclose(
            actual, expected, rel_tol=0, abs_tol=1e-12
        ):
            errors.append(f"community {community_id}: average ER mismatch")

    criteria = competitor_set.get("criteria", {})
    if competitor_set.get("window", {}).get("duration_hours") != 31 * 24:
        errors.append("result window must equal 31×24 hours")
    target = criteria.get("target_count")
    target_met = competitor_set.get("validation", {}).get("target_met")
    if target_met and len(competitors) != target:
        errors.append("target_met is inconsistent")
    if not target_met and isinstance(target, int) and len(competitors) >= target:
        errors.append("shortfall marker is inconsistent")
    if competitor_set.get("validation", {}).get("conditions_relaxed") is not False:
        errors.append("conditions_relaxed must remain false")

    candidates = audit.get("candidates")
    if not isinstance(candidates, list):
        errors.append("audit candidates are missing")
    else:
        audit_ids = [item.get("community_id") for item in candidates]
        if len(audit_ids) != len(set(audit_ids)):
            errors.append("duplicate audit candidate IDs")
        for item in candidates:
            if item.get("decision") == "excluded" and not (
                item.get("exclusion_code") and item.get("exclusion_reason")
            ):
                errors.append("excluded candidate lacks a concrete reason")
                break
        if audit.get("counts", {}).get("total") != len(candidates):
            errors.append("audit total mismatch")
        raw_ids = {
            int(item.get("id") or item.get("community_id") or 0)
            for item in raw.get("candidates", [])
            if int(item.get("id") or item.get("community_id") or 0) > 0
        }
        if set(audit_ids) != raw_ids:
            errors.append("audit does not cover every discovered candidate")

    xlsx_path = directory / XLSX_NAME
    if not xlsx_path.is_file():
        errors.append(f"missing {XLSX_NAME}")
    else:
        try:
            if workbook_sheet_names(xlsx_path) != SHEET_NAMES:
                errors.append("Excel sheet names mismatch")
            rows = workbook_rows(xlsx_path)
            competitor_rows = rows.get("Конкуренты", [])
            if len(competitor_rows) < 4 or competitor_rows[3] != COMPETITOR_HEADERS:
                errors.append("Excel competitor headers mismatch")
            else:
                data_rows = competitor_rows[4:]
                if len(data_rows) != len(competitors):
                    errors.append("Excel competitor row count mismatch")
                else:
                    for row, item in zip(data_rows, competitors):
                        expected = [
                            item.get("name"),
                            item.get("vk"),
                            item.get("site"),
                            item.get("competitor_type"),
                            item.get("followers"),
                            item.get("last_own_publication_at"),
                            item.get("average_er"),
                            item.get("assortment"),
                            item.get("audience"),
                            item.get("geography"),
                            item.get("price_segment"),
                            item.get("sales_model"),
                            item.get("production"),
                            item.get("online_offline"),
                            item.get("why_competitor"),
                            "\n".join(item.get("verified_sources", [])),
                        ]
                        if row != expected:
                            errors.append("Excel competitor data mismatch")
                            break
            excluded_rows = rows.get("Проверены, но исключены", [])[4:]
            expected_excluded = sum(
                item.get("decision") == "excluded"
                for item in audit.get("candidates", [])
            )
            if len(excluded_rows) != expected_excluded:
                errors.append("Excel exclusion row count mismatch")
            post_rows = rows.get("Посты за месяц", [])[4:]
            if len(post_rows) != len(posts):
                errors.append("Excel post row count mismatch")
            else:
                for row, post in zip(post_rows, posts):
                    if (
                        row[0] != post.get("community_id")
                        or row[3] != post.get("post_id")
                        or row[9] != post.get("views")
                        or row[11] != post.get("er")
                        or row[12] != post.get("er_exclusion_reason")
                    ):
                        errors.append("Excel post data mismatch")
                        break
        except (zipfile.BadZipFile, KeyError):
            errors.append("invalid Excel file")
        expected_xlsx_hash = passport.get("output_hashes", {}).get(XLSX_NAME)
        if expected_xlsx_hash != sha256_file(xlsx_path):
            errors.append("Excel hash mismatch")
    expected_audit_hash = passport.get("output_hashes", {}).get(AUDIT_NAME)
    if expected_audit_hash != sha256_file(directory / AUDIT_NAME):
        errors.append("audit hash mismatch")
    expected_set_hash = passport.get("output_hashes", {}).get(JSON_NAME)
    if expected_set_hash != competitor_set_hash:
        errors.append("competitor_set hash mismatch")

    for name in (
        JSON_NAME,
        AUDIT_NAME,
        PASSPORT_NAME,
        RAW_NAME,
        MEASURED_NAME,
        REVIEWS_NAME,
        XLSX_NAME,
        HASH_NAME,
    ):
        path = directory / name
        if path.is_file() and scan_file_for_secrets(path):
            errors.append(f"{name}: potential secret detected")
    return list(dict.fromkeys(errors))
