from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from vk_api import dedupe_candidates, response_items


VK_HOSTS = {"vk.com", "www.vk.com", "vk.ru", "www.vk.ru", "m.vk.com", "m.vk.ru"}
COMMUNITY_TYPES = {"group", "page", "event"}
COMPETITOR_TYPES = {"direct", "indirect", "attention"}
COMPETITOR_LABELS = {
    "direct": "Прямой",
    "indirect": "Косвенный",
    "attention": "За внимание",
}
REQUIRED_SOURCE_SUPPORT = {
    "assortment",
    "audience",
    "geography",
    "price",
    "sales",
    "production",
    "online_offline",
}
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]{3,}")
RUSSIA_WIDE_MARKERS = (
    "росси",
    "онлайн",
    "дистанц",
    "весь мир",
    "без огранич",
)


class WorkflowError(RuntimeError):
    pass


def text(value: Any) -> str:
    return str(value or "").strip()


def strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text(item) for item in value if text(item)]


def first_nonempty(*values: Any) -> str:
    for value in values:
        candidate = text(value)
        if candidate:
            return candidate
    return ""


def context_primary_offers(context: dict[str, Any]) -> list[str]:
    assortment = context.get("assortment") or {}
    offers = assortment.get("primary_offers") or []
    result: list[str] = []
    for offer in offers:
        if isinstance(offer, dict):
            name = text(offer.get("name"))
        else:
            name = text(offer)
        if name:
            result.append(name)
    if not result:
        result.extend(strings(assortment.get("primary")))
    return list(dict.fromkeys(result))


def context_audience(context: dict[str, Any]) -> list[str]:
    audience = context.get("audience") or {}
    result = strings(audience.get("primary_payers"))
    for segment in audience.get("segments") or []:
        if isinstance(segment, dict) and text(segment.get("name")):
            result.append(text(segment["name"]))
    return list(dict.fromkeys(result))


def normalize_context(context: dict[str, Any]) -> dict[str, Any]:
    competitors = context.get("competitors")
    if not isinstance(competitors, dict):
        raise WorkflowError("competitors object is required")
    constraints = competitors.get("search_constraints")
    if not isinstance(constraints, dict):
        raise WorkflowError("competitors.search_constraints object is required")

    target = constraints.get(
        "target_count",
        constraints.get("target_total"),
    )
    if not isinstance(target, int) or target <= 0:
        raise WorkflowError("positive target_count or target_total is required")

    followers = constraints.get(
        "audience_size",
        constraints.get("vk_followers"),
    )
    if not isinstance(followers, dict):
        raise WorkflowError("audience_size or vk_followers is required")
    minimum = followers.get("min")
    maximum = followers.get("max")
    minimum = 0 if minimum is None else minimum
    maximum = 2_147_483_647 if maximum is None else maximum
    if (
        not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 0
        or maximum < minimum
    ):
        raise WorkflowError("invalid follower range")

    geography_object = context.get("geography") or {}
    primary_geography = first_nonempty(
        constraints.get("geography"),
        geography_object.get("primary"),
        geography_object.get("primary_city"),
    )
    if not primary_geography:
        raise WorkflowError("search geography is required")

    freshness_days = constraints.get("freshness_days")
    if isinstance(freshness_days, (int, float)) and freshness_days > 0:
        activity_hours = int(freshness_days * 24)
    else:
        freshness = constraints.get("freshness")
        if not isinstance(freshness, dict):
            raise WorkflowError("freshness_days or freshness object is required")
        if freshness.get("window_hours"):
            activity_hours = int(freshness["window_hours"])
        elif freshness.get("days"):
            activity_hours = int(freshness["days"] * 24)
        elif freshness.get("max_age_days"):
            activity_hours = int(freshness["max_age_days"] * 24)
        elif freshness.get("period_months"):
            activity_hours = int(freshness["period_months"] * 31 * 24)
        else:
            raise WorkflowError("freshness must be positive")
    if activity_hours <= 0:
        raise WorkflowError("freshness must be positive")

    platforms = strings(constraints.get("platforms"))
    if platforms and not any("vk" in item.casefold() for item in platforms):
        raise WorkflowError("VK must be an enabled search platform")
    queries = strings(competitors.get("proposed_search_queries"))
    if not queries:
        raise WorkflowError("competitors.proposed_search_queries must not be empty")

    matching_policy = competitors.get("matching_policy")
    if not isinstance(matching_policy, dict):
        matching_policy = {
            "must_repeat_in_competitors": False,
            "importance": "informational",
            "applies_to": [],
            "rationale": "Not specified in legacy business context.",
        }
    matching_policy = {
        "must_repeat_in_competitors": bool(
            matching_policy.get("must_repeat_in_competitors")
        ),
        "importance": text(matching_policy.get("importance")) or "informational",
        "applies_to": [
            item
            for item in strings(matching_policy.get("applies_to"))
            if item in COMPETITOR_TYPES
        ],
        "rationale": text(matching_policy.get("rationale"))
        or "Not specified.",
    }

    business = context.get("business") or {}
    broader_categories = strings(business.get("broader_categories"))
    if not broader_categories:
        broader_categories = strings(business.get("categories"))
    sales_formats = strings(geography_object.get("sales_format"))
    if not sales_formats:
        sales = context.get("sales") or {}
        sales_formats = strings(sales.get("formats"))

    return {
        "schema_version": text(context.get("schema_version")) or "legacy",
        "target_count": target,
        "pool_target": target * 3,
        "followers_min": minimum,
        "followers_max": maximum,
        "followers_inclusive": bool(followers.get("inclusive", True)),
        "primary_geography": primary_geography,
        "geography_rule": constraints.get("geography"),
        "activity_freshness_hours": activity_hours,
        "metric_window_hours": 31 * 24,
        "required_inclusions": strings(constraints.get("required_inclusions")),
        "exclusions": strings(constraints.get("exclusions")),
        "queries": queries,
        "known": list(competitors.get("known") or []),
        "classification_rules": dict(competitors.get("classification_rules") or {}),
        "classification_factors": strings(
            competitors.get("classification_factors")
        ),
        "key_features": strings(competitors.get("key_features")),
        "matching_policy": matching_policy,
        "business_name": text(business.get("name")),
        "niche": text(business.get("marketing_niche")),
        "broader_categories": broader_categories,
        "primary_offers": context_primary_offers(context),
        "audience": context_audience(context),
        "sales_formats": sales_formats,
        "production": dict(context.get("production") or {}),
        "pricing": dict(context.get("pricing") or {}),
        "allowed_community_types": sorted(COMMUNITY_TYPES),
    }


def validate_business_context_document(context: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if context.get("status") != "confirmed":
        errors.append("business_context.json must have status: confirmed")
    if context.get("confirmation_date") is None and context.get("confirmed_at") is None:
        errors.append("confirmed business context needs a confirmation date")
    try:
        normalize_context(context)
    except WorkflowError as exc:
        errors.append(str(exc))
    return errors


def known_inputs(context: dict[str, Any]) -> list[dict[str, Any]]:
    config = normalize_context(context)
    result: list[dict[str, Any]] = []
    for item in config["known"]:
        if isinstance(item, str):
            result.append({"name": item, "url": None, "origin": "known"})
        elif isinstance(item, dict):
            notes = text(item.get("notes"))
            note_vk_match = re.search(
                r"https?://(?:www\.|m\.)?vk\.(?:com|ru)/[^\s;,)]+",
                notes,
                flags=re.IGNORECASE,
            )
            note_vk_url = note_vk_match.group(0) if note_vk_match else None
            result.append(
                {
                    "name": text(item.get("name")),
                    "url": first_nonempty(
                        item.get("vk"),
                        item.get("vk_url"),
                        note_vk_url,
                        item.get("url"),
                    )
                    or None,
                    "origin": "known",
                }
            )
    for item in config["required_inclusions"]:
        parsed = urlparse(item)
        result.append(
            {
                "name": "" if parsed.scheme else item,
                "url": item if parsed.scheme else None,
                "origin": "required_inclusion",
            }
        )
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in result:
        key = (
            text(item.get("name")).casefold(),
            text(item.get("url")).casefold(),
            item["origin"],
        )
        if key[0] or key[1]:
            unique.setdefault(key, item)
    return list(unique.values())


def generated_query_plan(context: dict[str, Any]) -> list[dict[str, str]]:
    config = normalize_context(context)
    rows: list[tuple[str, str]] = [
        (query, "business_context") for query in config["queries"]
    ]
    for item in known_inputs(context):
        if item.get("name"):
            rows.append((text(item["name"]), item["origin"]))
    geography = config["primary_geography"]
    for offer in config["primary_offers"]:
        rows.extend(
            [
                (f"{offer} {geography}", "generated_offer_geography"),
                (offer, "generated_offer"),
            ]
        )
    if config["niche"]:
        rows.extend(
            [
                (
                    f"{config['niche']} {geography}",
                    "generated_niche_geography",
                ),
                (config["niche"], "generated_niche"),
            ]
        )
    for category in config["broader_categories"]:
        rows.append(
            (
                f"{category} {geography}",
                "generated_category_geography",
            )
        )
    seen: set[str] = set()
    plan: list[dict[str, str]] = []
    for query, origin in rows:
        query = " ".join(text(query).split())
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            plan.append({"query": query, "origin": origin})
    return plan


def discover_with_saturation(
    call: Callable[[str, dict[str, Any]], Any],
    query_plan: Iterable[dict[str, str]],
    *,
    pool_target: int,
    growth_threshold: float = 0.05,
    low_growth_limit: int = 2,
    page_size: int = 1000,
    max_pages_per_query: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if page_size != 1000:
        raise WorkflowError("groups.search page size must be 1000")
    discovered: list[dict[str, Any]] = []
    query_runs: list[dict[str, Any]] = []
    consecutive_low_growth = 0
    stop_reason = "query_plan_exhausted"

    for plan_item in query_plan:
        query = text(plan_item.get("query"))
        if not query:
            continue
        before_ids = {
            int(item.get("id", 0)) for item in discovered if int(item.get("id", 0)) > 0
        }
        offset = 0
        pages = 0
        returned = 0
        for _ in range(max_pages_per_query):
            pages += 1
            total, items = response_items(
                call(
                    "groups.search",
                    {
                        "q": query,
                        "count": 1000,
                        "offset": offset,
                        "sort": 0,
                    },
                )
            )
            returned += len(items)
            for item in items:
                enriched = dict(item)
                enriched["matched_queries"] = sorted(
                    set(enriched.get("matched_queries", [])) | {query}
                )
                enriched["query_origins"] = sorted(
                    set(enriched.get("query_origins", []))
                    | {text(plan_item.get("origin"))}
                )
                discovered.append(enriched)
            if not items or offset + len(items) >= total:
                break
            offset += len(items)

        unique = dedupe_candidates(discovered)
        after_ids = {int(item["id"]) for item in unique}
        added = len(after_ids - before_ids)
        growth_rate = 1.0 if not before_ids and added else (
            added / max(len(before_ids), 1)
        )
        low_growth = growth_rate < growth_threshold
        consecutive_low_growth = (
            consecutive_low_growth + 1 if low_growth else 0
        )
        query_runs.append(
            {
                "query": query,
                "origin": text(plan_item.get("origin")),
                "pages": pages,
                "returned": returned,
                "unique_before": len(before_ids),
                "new_unique": added,
                "unique_after": len(after_ids),
                "growth_rate": growth_rate,
                "low_growth": low_growth,
            }
        )
        discovered = unique
        if len(after_ids) >= pool_target:
            stop_reason = "pool_reached_three_per_target"
            break
        if consecutive_low_growth >= low_growth_limit:
            stop_reason = "two_consecutive_queries_below_five_percent"
            break

    return dedupe_candidates(discovered), {
        "growth_threshold": growth_threshold,
        "low_growth_limit": low_growth_limit,
        "pool_target": pool_target,
        "stop_reason": stop_reason,
        "query_runs": query_runs,
        "queries_executed": len(query_runs),
        "unique_candidates": len(dedupe_candidates(discovered)),
    }


def normalize_city(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("title") or value.get("name")
    return text(value)


def geography_is_broad(config: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            config["primary_geography"],
            *config.get("sales_formats", []),
        ]
    ).casefold()
    return any(marker in haystack for marker in RUSSIA_WIDE_MARKERS)


def exact_reference_match(candidate: dict[str, Any], values: Iterable[str]) -> bool:
    candidate_values = {
        text(candidate.get("name")).casefold(),
        text(candidate.get("screen_name")).casefold(),
        text(candidate.get("domain")).casefold(),
        f"https://vk.com/{text(candidate.get('screen_name') or candidate.get('domain'))}".casefold(),
        f"https://vk.ru/{text(candidate.get('screen_name') or candidate.get('domain'))}".casefold(),
    }
    for value in values:
        needle = text(value).rstrip("/").casefold()
        if needle and (
            needle in candidate_values
            or needle.split("/")[-1] in candidate_values
        ):
            return True
    return False


def prefilter_candidates(
    candidates: Iterable[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    processed: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    primary_geography = config["primary_geography"].casefold()
    broad_geography = geography_is_broad(config)

    for raw in dedupe_candidates(candidates):
        candidate = dict(raw)
        community_id = int(candidate.get("id", 0))
        required_names = {
            text(item).casefold() for item in config["required_inclusions"]
        }
        matched_known_names = {
            text(query).split(":", 1)[1].strip().casefold()
            for query in candidate.get("matched_queries", [])
            if text(query).casefold().startswith("known:")
            and ":" in text(query)
        }
        candidate["required_inclusion"] = exact_reference_match(
            candidate,
            config["required_inclusions"],
        ) or "required_inclusion" in candidate.get("query_origins", []) or bool(
            required_names & matched_known_names
        )
        candidate["known_competitor"] = (
            "known" in candidate.get("query_origins", [])
            or "known_competitor" in candidate.get("matched_queries", [])
        )
        stage = code = reason = None
        if (
            community_id <= 0
            or candidate.get("deactivated")
            or int(candidate.get("is_closed") or 0) != 0
        ):
            stage, code, reason = (
                "availability",
                "vk_unavailable",
                "VK-сообщество закрыто, удалено или недоступно для проверки",
            )
        community_type = text(candidate.get("type")) or "group"
        if stage is None and community_type not in config["allowed_community_types"]:
            stage, code, reason = (
                "community_type",
                "unsupported_community_type",
                f"Тип VK-сообщества {community_type!r} не поддерживается",
            )
        try:
            followers = int(candidate.get("members_count"))
        except (TypeError, ValueError):
            followers = -1
        if stage is None:
            if config["followers_inclusive"]:
                followers_ok = (
                    config["followers_min"]
                    <= followers
                    <= config["followers_max"]
                )
            else:
                followers_ok = (
                    config["followers_min"]
                    < followers
                    < config["followers_max"]
                )
            if not followers_ok and not candidate["required_inclusion"]:
                stage, code, reason = (
                    "followers",
                    "followers_out_of_range",
                    (
                        f"Подписчики {followers} не входят в диапазон "
                        f"{config['followers_min']}–{config['followers_max']}"
                    ),
                )
        city = normalize_city(candidate.get("city"))
        if (
            stage is None
            and city
            and not broad_geography
            and city.casefold() not in primary_geography
            and primary_geography not in city.casefold()
        ):
            stage, code, reason = (
                "geography",
                "geography_mismatch",
                (
                    f"География сообщества {city!r} не соответствует "
                    f"{config['primary_geography']!r}"
                ),
            )
        candidate["prefilter"] = {
            "passed": stage is None,
            "stage": stage,
            "code": code,
            "reason": reason,
            "community_type": community_type,
            "city": city or None,
        }
        audit.append(
            {
                "community_id": community_id,
                "passed": stage is None,
                "stage": stage,
                "code": code,
                "reason": reason,
            }
        )
        processed.append(candidate)
    return processed, audit


def words(value: Any) -> set[str]:
    return {item.casefold() for item in WORD_RE.findall(text(value))}


def candidate_rank(candidate: dict[str, Any], config: dict[str, Any]) -> float:
    business_words: set[str] = set()
    for value in (
        config["niche"],
        config["primary_geography"],
        *config["primary_offers"],
        *config["broader_categories"],
        *config["audience"],
        *config["key_features"],
    ):
        business_words.update(words(value))
    candidate_text = " ".join(
        [
            text(candidate.get("name")),
            text(candidate.get("description")),
            text(candidate.get("status")),
            text(candidate.get("activity")),
            " ".join(candidate.get("matched_queries", [])),
        ]
    )
    overlap = len(business_words & words(candidate_text))
    score = float(overlap * 5 + len(candidate.get("matched_queries", [])) * 8)
    if candidate.get("required_inclusion"):
        score += 10_000
    if candidate.get("known_competitor"):
        score += 5_000
    if normalize_city(candidate.get("city")).casefold() in config[
        "primary_geography"
    ].casefold():
        score += 50
    if text(candidate.get("site")):
        score += 5
    followers = int(candidate.get("members_count") or 0)
    midpoint = (config["followers_min"] + config["followers_max"]) / 2
    if config["followers_max"] > config["followers_min"]:
        score += max(
            0.0,
            10.0
            * (
                1
                - abs(followers - midpoint)
                / (config["followers_max"] - config["followers_min"])
            ),
        )
    return round(score, 6)


def compact_business_context(
    context: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    return {
        "business": {
            "name": config["business_name"],
            "niche": config["niche"],
            "primary_offers": config["primary_offers"],
            "audience": config["audience"],
            "geography": config["primary_geography"],
            "pricing": config["pricing"],
            "production": config["production"],
            "sales_formats": config["sales_formats"],
        },
        "classification_rules": config["classification_rules"],
        "classification_factors": config["classification_factors"],
        "key_features": config["key_features"],
        "matching_policy": config["matching_policy"],
        "required_inclusions": config["required_inclusions"],
        "exclusions": config["exclusions"],
    }


def compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    screen_name = text(
        candidate.get("screen_name")
        or candidate.get("domain")
        or f"club{candidate.get('id')}"
    )
    links: list[str] = []
    for item in candidate.get("links") or []:
        if isinstance(item, dict) and text(item.get("url")):
            links.append(text(item["url"]))
    return {
        "community_id": int(candidate["id"]),
        "name": text(candidate.get("name")),
        "vk": f"https://vk.com/{screen_name}",
        "site_claim": text(candidate.get("site")) or None,
        "community_type": text(candidate.get("type")) or "group",
        "followers": int(candidate.get("members_count") or 0),
        "city": normalize_city(candidate.get("city")) or None,
        "activity": text(candidate.get("activity"))[:160],
        "status": text(candidate.get("status"))[:240],
        "description": text(candidate.get("description"))[:700],
        "links": links[:8],
        "matched_queries": list(candidate.get("matched_queries", []))[:12],
        "known_competitor": bool(candidate.get("known_competitor")),
        "required_inclusion": bool(candidate.get("required_inclusion")),
        "rank_score": candidate.get("rank_score"),
    }


def build_review_batches(
    context: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    *,
    business_context_sha256: str,
    raw_candidates_sha256: str,
    batch_size: int = 200,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not 1 <= batch_size <= 200:
        raise WorkflowError("semantic review batch_size must be between 1 and 200")
    config = normalize_context(context)
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        item["rank_score"] = candidate_rank(item, config)
        ranked.append(item)
    ranked.sort(
        key=lambda item: (
            not bool(item.get("required_inclusion")),
            not bool(item.get("known_competitor")),
            -float(item["rank_score"]),
            -int(item.get("members_count") or 0),
            int(item["id"]),
        )
    )
    batches: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    compact_context = compact_business_context(context, config)
    for start in range(0, len(ranked), batch_size):
        index = start // batch_size + 1
        items = ranked[start : start + batch_size]
        name = f"batch-{index:03d}.json"
        review_name = f"review-{index:03d}.json"
        batches.append(
            {
                "schema_version": "1.1",
                "batch_index": index,
                "business_context_sha256": business_context_sha256,
                "raw_candidates_sha256": raw_candidates_sha256,
                "business_context": compact_context,
                "candidates": [compact_candidate(item) for item in items],
                "review_output": review_name,
            }
        )
        summary_rows.append(
            {
                "batch_index": index,
                "file": name,
                "review_output": review_name,
                "candidate_count": len(items),
                "candidate_ids": [int(item["id"]) for item in items],
            }
        )
    manifest = {
        "schema_version": "1.1",
        "business_context_sha256": business_context_sha256,
        "raw_candidates_sha256": raw_candidates_sha256,
        "batch_size": batch_size,
        "total_candidates": len(ranked),
        "total_batches": len(batches),
        "processing_rule": (
            "Process batches in order. Open the next batch only when the number "
            "of qualified, fresh competitors remains below target_count."
        ),
        "batches": summary_rows,
    }
    return manifest, batches


def source_url_is_vk(value: Any) -> bool:
    try:
        parsed = urlparse(text(value))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() in VK_HOSTS


def source_origin(value: Any) -> str:
    try:
        parsed = urlparse(text(value))
    except ValueError:
        return ""
    return parsed.netloc.casefold().removeprefix("www.")


def validate_review_document(
    reviews: dict[str, Any],
    *,
    candidates_by_id: dict[int, dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    if reviews.get("schema_version") not in {"1.0", "1.1"}:
        errors.append("candidate_reviews.json has unsupported schema_version")
    if not text(reviews.get("generated_at")):
        errors.append("candidate_reviews.json missing generated_at")
    items = reviews.get("candidates")
    if not isinstance(items, list):
        return [*errors, "candidate_reviews.json candidates must be an array"]
    seen: set[int] = set()
    for index, item in enumerate(items):
        label = f"candidate_reviews.json candidates[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        community_id = item.get("community_id")
        if not isinstance(community_id, int) or community_id <= 0:
            errors.append(f"{label} has invalid community_id")
            continue
        if community_id in seen:
            errors.append(f"{label} duplicates community_id {community_id}")
        seen.add(community_id)
        candidate = (candidates_by_id or {}).get(community_id)
        if candidates_by_id is not None and candidate is None:
            errors.append(f"{label} is not present in raw_candidates.json")
        decision = item.get("decision")
        if decision not in {"include", "exclude"}:
            errors.append(f"{label} has invalid decision")
            continue
        reason = text(item.get("reason"))
        if not reason:
            errors.append(f"{label} has no concrete reason")
        source_checks = item.get("source_checks")
        if not isinstance(source_checks, list) or not source_checks:
            errors.append(f"{label} has no source_checks")
            source_checks = []
        official_vk = [
            source
            for source in source_checks
            if isinstance(source, dict)
            and source.get("kind") == "official_vk"
            and source.get("official") is True
            and source_url_is_vk(source.get("url"))
        ]
        if not official_vk:
            errors.append(f"{label} has no verified official VK source")
        for source_index, source in enumerate(source_checks):
            if not isinstance(source, dict):
                errors.append(f"{label} source_checks[{source_index}] must be an object")
                continue
            if source.get("kind") not in {
                "official_vk",
                "official_website",
                "primary_other",
            }:
                errors.append(f"{label} source_checks[{source_index}] has invalid kind")
            if source.get("official") is not True:
                errors.append(f"{label} source_checks[{source_index}] is not primary")
            if not text(source.get("url")).startswith(("http://", "https://")):
                errors.append(f"{label} source_checks[{source_index}] has invalid URL")
            try:
                datetime.fromisoformat(
                    text(source.get("checked_at")).replace("Z", "+00:00")
                )
            except ValueError:
                errors.append(
                    f"{label} source_checks[{source_index}] has invalid checked_at"
                )
        site = text(item.get("site"))
        if site:
            site_origin = source_origin(site)
            website_checks = [
                source
                for source in source_checks
                if isinstance(source, dict)
                and source.get("kind") == "official_website"
                and source.get("official") is True
                and source_origin(source.get("url")) == site_origin
            ]
            if not website_checks:
                errors.append(f"{label} site is not verified by an official source")
        if decision == "exclude":
            if not text(item.get("exclusion_code")):
                errors.append(f"{label} exclusion_code is required")
            continue

        competitor_type = item.get("competitor_type")
        if competitor_type not in COMPETITOR_TYPES:
            errors.append(f"{label} has invalid competitor_type")
            continue
        for key in (
            "assortment",
            "audience",
            "price_segment",
            "sales_model",
            "production",
            "online_offline",
        ):
            if not text(item.get(key)):
                errors.append(f"{label} missing {key} for inclusion")
        if item.get("geography_match") is not True:
            errors.append(f"{label} geography is not confirmed")
        evidence = item.get("classification_evidence")
        if not isinstance(evidence, dict):
            errors.append(f"{label} missing classification_evidence")
            evidence = {}
        if competitor_type == "direct":
            if not all(
                evidence.get(key) is True
                for key in (
                    "same_primary_product",
                    "same_audience",
                    "same_specialization",
                )
            ):
                errors.append(f"{label} lacks direct-competitor evidence")
            coverage = evidence.get("primary_offer_coverage_ratio")
            if not isinstance(coverage, (int, float)) or not 0.5 < coverage <= 1:
                errors.append(
                    f"{label} direct competitor must cover a majority of primary offers"
                )
        elif competitor_type == "indirect":
            if evidence.get("partial_or_similar_assortment") is not True:
                errors.append(f"{label} lacks indirect-competitor evidence")
        elif competitor_type == "attention":
            if not (
                evidence.get("similar_need") is True
                and evidence.get("substitute_or_complement") is True
            ):
                errors.append(f"{label} lacks attention-competitor evidence")

        feature_check = item.get("key_feature_check")
        if not isinstance(feature_check, dict):
            errors.append(f"{label} missing key_feature_check")
            feature_check = {}
        if config:
            policy = config["matching_policy"]
            applies = competitor_type in policy["applies_to"]
            must_match = policy["must_repeat_in_competitors"] and applies
            if must_match and feature_check.get("matches") is not True:
                errors.append(f"{label} violates required key-feature matching")
        constraint_check = item.get("constraint_check")
        if not isinstance(constraint_check, dict):
            errors.append(f"{label} missing constraint_check")
            constraint_check = {}
        matched_exclusions = constraint_check.get("matched_exclusions")
        if isinstance(matched_exclusions, list) and matched_exclusions:
            errors.append(f"{label} matches an explicit exclusion")

        supports: set[str] = set()
        for source in source_checks:
            if isinstance(source, dict):
                supports.update(strings(source.get("supports")))
        missing_supports = REQUIRED_SOURCE_SUPPORT - supports
        if missing_supports:
            errors.append(
                f"{label} sources do not support: {', '.join(sorted(missing_supports))}"
            )
    return errors


def merge_review_documents(documents: Iterable[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    processed_batches: list[int] = []
    for document in documents:
        candidates.extend(document.get("candidates") or [])
        batch = document.get("batch_index")
        if isinstance(batch, int):
            processed_batches.append(batch)
        processed_batches.extend(
            item
            for item in document.get("processed_batches") or []
            if isinstance(item, int)
        )
    seen: set[int] = set()
    merged: list[dict[str, Any]] = []
    for item in candidates:
        community_id = int(item.get("community_id", 0))
        if community_id <= 0 or community_id in seen:
            raise WorkflowError(
                f"duplicate or invalid candidate review ID: {community_id}"
            )
        seen.add(community_id)
        merged.append(item)
    return {
        "schema_version": "1.1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "processed_batches": sorted(set(processed_batches)),
        "candidates": merged,
    }


def next_review_batch(
    manifest: dict[str, Any],
    reviews: dict[str, Any],
    *,
    qualified_count: int,
    target_count: int,
) -> int | None:
    processed = set(reviews.get("processed_batches") or [])
    if qualified_count >= target_count:
        return None
    for batch in manifest.get("batches") or []:
        index = batch.get("batch_index")
        if isinstance(index, int) and index not in processed:
            return index
    return None


def save_review_batches(
    directory: Path,
    manifest: dict[str, Any],
    batches: Iterable[dict[str, Any]],
    *,
    writer: Callable[[Path, Any], None],
) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    writer(directory / "manifest.json", manifest)
    for batch in batches:
        index = int(batch["batch_index"])
        writer(directory / f"batch-{index:03d}.json", batch)
