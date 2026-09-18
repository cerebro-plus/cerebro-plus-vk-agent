from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

from pipeline import (  # noqa: E402
    PipelineError,
    build_measured_candidates,
    confirm_outputs,
    finalize_outputs,
    load_json,
    search_config,
    sha256_file,
    validate_output_directory,
    write_json,
)
from vk_api import (  # noqa: E402
    RetryPolicy,
    VKClient,
    VKPreflightError,
    collect_wall_posts,
    dedupe_candidates,
    dedupe_posts,
)
from workflow import (  # noqa: E402
    build_review_batches,
    discover_with_saturation,
    generated_query_plan,
    normalize_context,
    prefilter_candidates,
    validate_review_document,
)
from xlsx_writer import SHEET_NAMES, workbook_rows, workbook_sheet_names  # noqa: E402


WINDOW_START = "2026-01-01T00:00:00Z"
WINDOW_END = "2026-02-01T00:00:00Z"
START_EPOCH = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
END_EPOCH = int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp())


def sample_context(
    *,
    status: str = "confirmed",
    target: int = 1,
    required_feature: bool = True,
) -> dict:
    return {
        "schema_version": "1.1",
        "status": status,
        "confirmation_date": "2025-12-31" if status == "confirmed" else None,
        "business": {
            "name": "Example Studio",
            "description": "Creative services for families.",
            "marketing_niche": "Creative services",
            "broader_categories": ["Family activities"],
            "positioning": "Local studio",
            "main_differentiator": "Own production",
        },
        "assortment": {
            "breadth": "Focused",
            "variation_dimensions": ["Format"],
            "primary_offers": [
                {"name": "Creative workshop", "formats": ["offline"], "priority": "primary"},
                {"name": "Family event", "formats": ["offline"], "priority": "primary"},
                {"name": "School event", "formats": ["offline"], "priority": "primary"},
            ],
            "secondary_offers": [],
            "product_categories": [],
        },
        "production": {
            "model": "Own service",
            "creators_or_suppliers": "Own team",
            "process": [],
            "capabilities": [],
            "lead_time": "One week",
            "notes": None,
        },
        "audience": {
            "primary_payers": ["Parents"],
            "segments": [{"name": "Families", "tasks": ["Family leisure"]}],
        },
        "geography": {
            "primary": "Example City",
            "additional": [],
            "sales_format": ["offline"],
        },
        "pricing": {"currency": "RUB", "structure": "Per event", "offers": []},
        "sales": {
            "booking_channels": ["Website"],
            "order_process": "Request",
            "prepayment_required": True,
            "payment_timing": "Before event",
            "payment_methods": [],
            "access_or_delivery": "At venue",
        },
        "acquisition": {"primary_channels": ["VK"], "additional_channels": []},
        "online_presence": {
            "vk_url": "https://vk.com/example_business",
            "website_url": "https://business.example",
            "other_links": [],
        },
        "advantages": ["Own production"],
        "limitations": [],
        "competitors": {
            "known": [
                {
                    "name": "Known Studio",
                    "url": "https://vk.com/known_studio",
                    "type": "direct",
                    "notes": None,
                }
            ],
            "classification_rules": {
                "direct": "Same product and audience, majority of offers.",
                "indirect": "Partial or similar assortment.",
                "attention": "Similar need and substitute or complement.",
            },
            "classification_factors": [
                "Assortment",
                "Audience",
                "Geography",
                "Price",
                "Sales",
                "Production",
                "Online/offline",
            ],
            "key_features": ["Own production"],
            "matching_policy": {
                "must_repeat_in_competitors": required_feature,
                "importance": "required" if required_feature else "preferred",
                "applies_to": ["direct"],
                "rationale": "User requirement",
            },
            "search_constraints": {
                "platforms": ["VK"],
                "target_count": target,
                "geography": "Example City",
                "audience_size": {"min": 100, "max": 10000},
                "freshness_days": 31,
                "required_inclusions": ["Known Studio"],
                "exclusions": ["Franchises"],
            },
            "proposed_search_queries": ["creative studio example city"],
        },
        "remaining_gaps": [],
        "sources": {
            "vk": {
                "url": "https://vk.com/example_business",
                "retrieved_at": "2025-12-31",
                "method": "VK API",
                "coverage": ["profile"],
                "limitations": [],
            },
            "website": None,
            "additional": [],
        },
        "security": {"secrets_included": False},
    }


def legacy_context() -> dict:
    return {
        "schema_version": "1.0",
        "status": "confirmed",
        "confirmation_date": "2025-12-31",
        "business": {"name": "Legacy", "marketing_niche": "Studio"},
        "geography": {"primary_city": "Example City"},
        "competitors": {
            "known": [],
            "search_constraints": {
                "platforms": ["VK"],
                "target_total": 2,
                "geography": "Example City",
                "vk_followers": {"min": 0, "max": 5000, "inclusive": True},
                "freshness": {"period_months": 1},
                "required_inclusions": [],
                "exclusions": [],
            },
            "proposed_search_queries": ["legacy studio"],
        },
        "security": {"secrets_included": False},
    }


def make_post(
    post_id: int,
    *,
    owner_id: int = -1001,
    date: int = END_EPOCH - 3600,
    views: int | None = 100,
    pinned: bool = False,
) -> dict:
    post = {
        "id": post_id,
        "owner_id": owner_id,
        "from_id": owner_id,
        "date": date,
        "likes": {"count": 3},
        "comments": {"count": 1},
        "reposts": {"count": 0},
        "text": "Owner post",
        "is_pinned": int(pinned),
    }
    if views is not None:
        post["views"] = {"count": views}
    return post


def candidate(
    community_id: int = 1001,
    *,
    name: str = "Example Competitor",
    city: str = "Example City",
    followers: int = 1200,
    community_type: str = "group",
) -> dict:
    return {
        "id": community_id,
        "name": name,
        "screen_name": f"example_{community_id}",
        "members_count": followers,
        "city": {"title": city},
        "site": "https://example.com",
        "type": community_type,
        "is_closed": 0,
        "matched_queries": ["creative studio example city"],
        "query_origins": ["business_context"],
        "description": "Creative workshop for families with own production.",
        "prefilter": {
            "passed": True,
            "stage": None,
            "code": None,
            "reason": None,
            "community_type": community_type,
            "city": city,
        },
        "rank_score": 100,
        "known_competitor": False,
        "required_inclusion": False,
    }


def valid_review(
    community_id: int = 1001,
    *,
    decision: str = "include",
    competitor_type: str | None = "direct",
) -> dict:
    return {
        "community_id": community_id,
        "decision": decision,
        "competitor_type": competitor_type,
        "geography_match": True,
        "geography": "Example City",
        "site": "https://example.com" if decision == "include" else None,
        "assortment": "Two of three primary offers.",
        "audience": "Families and parents.",
        "price_segment": "middle",
        "sales_model": "Advance booking.",
        "production": "Own service delivery.",
        "online_offline": "Offline with online booking.",
        "reason": (
            "Same product, audience and specialization."
            if decision == "include"
            else "Explicit mismatch."
        ),
        "exclusion_code": None if decision == "include" else "semantic_mismatch",
        "classification_evidence": {
            "same_primary_product": True,
            "same_audience": True,
            "same_specialization": True,
            "primary_offer_coverage_ratio": 2 / 3,
            "partial_or_similar_assortment": True,
            "similar_need": True,
            "substitute_or_complement": True,
        },
        "key_feature_check": {
            "policy_applies": True,
            "matches": True,
            "matched": ["Own production"],
            "missing": [],
            "rationale": "Confirmed on official VK.",
        },
        "constraint_check": {
            "matched_required_inclusion": False,
            "matched_exclusions": [],
        },
        "source_checks": [
            {
                "url": f"https://vk.com/example_{community_id}",
                "kind": "official_vk",
                "official": True,
                "checked_at": "2026-02-01T00:00:00Z",
                "supports": [
                    "assortment",
                    "audience",
                    "geography",
                    "price",
                    "sales",
                    "production",
                    "online_offline",
                    "key_features",
                ],
            },
            {
                "url": "https://example.com",
                "kind": "official_website",
                "official": True,
                "checked_at": "2026-02-01T00:00:00Z",
                "supports": ["assortment", "price", "sales"],
            },
        ],
        "priority_score": 10,
    }


def reviews_document(items: list[dict], batches: list[int] | None = None) -> dict:
    return {
        "schema_version": "1.1",
        "generated_at": "2026-02-01T00:00:00Z",
        "processed_batches": batches or [1],
        "candidates": items,
    }


def raw_document(context_hash: str, candidates: list[dict]) -> dict:
    return {
        "schema_version": "1.1",
        "generated_at": "2026-02-01T00:00:00Z",
        "provenance": {
            "business_context_file": "business_context.json",
            "business_context_sha256": context_hash,
        },
        "criteria_snapshot": {},
        "vk_api_version": "5.199",
        "vk_methods": ["groups.search", "groups.getById"],
        "search": {
            "stop_reason": "pool_reached_three_per_target",
            "query_runs": [],
        },
        "pagination": {"groups_search_page_size": 1000},
        "known_and_required_resolution": [],
        "prefilter_audit": [],
        "semantic_batches": {
            "batch_size": 200,
            "eligible_candidates": len(candidates),
            "total_batches": 1,
        },
        "retry_policy": {"attempts": 5},
        "vk_api_calls": 2,
        "errors": [],
        "limitations": [],
        "candidates": candidates,
    }


def measured_document(
    context_hash: str,
    raw_hash: str,
    reviews_hash: str,
    candidates: list[dict],
    posts_by_id: dict[int, list[dict]] | None = None,
) -> dict:
    posts_by_id = posts_by_id or {1001: [make_post(10)]}
    measured_candidates = []
    for item in candidates:
        community_id = int(item["id"])
        posts = posts_by_id.get(community_id, [])
        measured = deepcopy(item)
        measured["wall"] = {
            "community_id": community_id,
            "owner_id": -community_id,
            "pages_fetched": 1,
            "stop_reason": "wall_end",
            "latest_own_publication_epoch": max(
                (post["date"] for post in posts),
                default=None,
            ),
            "posts": posts,
        }
        measured_candidates.append(measured)
    return {
        "schema_version": "1.1",
        "generated_at": "2026-02-01T00:00:00Z",
        "provenance": {
            "business_context_file": "business_context.json",
            "business_context_sha256": context_hash,
            "raw_candidates_file": "raw_candidates.json",
            "raw_candidates_sha256": raw_hash,
            "candidate_reviews_file": "candidate_reviews.json",
            "candidate_reviews_sha256": reviews_hash,
        },
        "metric_window": {
            "timezone": "UTC",
            "start": WINDOW_START,
            "end": WINDOW_END,
            "duration_hours": 744,
            "inclusive": True,
        },
        "activity_window": {
            "timezone": "UTC",
            "start": WINDOW_START,
            "end": WINDOW_END,
            "duration_hours": 744,
            "inclusive": True,
        },
        "collection_window": {
            "timezone": "UTC",
            "start": WINDOW_START,
            "end": WINDOW_END,
            "inclusive": True,
        },
        "vk_api_version": "5.199",
        "vk_methods": ["wall.get"],
        "pagination": {"wall_page_size": 100, "rows": []},
        "retry_policy": {"attempts": 5},
        "vk_api_calls": 1,
        "errors": [],
        "limitations": [],
        "candidates": measured_candidates,
    }


class PipelineFixture:
    def __init__(
        self,
        root: Path,
        *,
        context: dict | None = None,
        candidates: list[dict] | None = None,
        reviews: list[dict] | None = None,
        posts_by_id: dict[int, list[dict]] | None = None,
    ) -> None:
        self.root = root
        self.context_path = root / "business_context.json"
        self.raw_path = root / "raw_candidates.json"
        self.reviews_path = root / "candidate_reviews.json"
        self.measured_path = root / "measured_candidates.json"
        self.output_dir = root / "result"
        write_json(self.context_path, context or sample_context())
        candidate_items = candidates or [candidate()]
        write_json(
            self.raw_path,
            raw_document(sha256_file(self.context_path), candidate_items),
        )
        write_json(
            self.reviews_path,
            reviews_document(reviews or [valid_review()]),
        )
        write_json(
            self.measured_path,
            measured_document(
                sha256_file(self.context_path),
                sha256_file(self.raw_path),
                sha256_file(self.reviews_path),
                candidate_items,
                posts_by_id,
            ),
        )

    def finalize(self) -> bool:
        return finalize_outputs(
            self.context_path,
            self.raw_path,
            self.measured_path,
            self.reviews_path,
            self.output_dir,
        )


class SearchVKCompetitorsTests(unittest.TestCase):
    def test_vk_preflight_stops_after_one_network_failure_with_reason_code(self) -> None:
        def blocked(_request, timeout):
            raise urllib.error.URLError(PermissionError(13, "socket access blocked"))

        client = VKClient(
            "test-token",
            opener=blocked,
            sleeper=lambda _seconds: None,
            retry_policy=RetryPolicy(attempts=5),
        )
        with self.assertRaises(VKPreflightError) as captured:
            client.preflight([("groups.search", {"q": "test", "count": 1})])
        self.assertEqual(captured.exception.reason_code, "network_blocked")
        self.assertEqual(client.calls, 1)

    def test_context_versions_are_supported_and_metric_window_is_fixed(self) -> None:
        modern = search_config(sample_context())
        legacy = search_config(legacy_context())
        self.assertEqual(744, modern["metric_window_hours"])
        self.assertEqual(744, legacy["metric_window_hours"])
        self.assertEqual(2, legacy["target_count"])

    def test_query_plan_adds_meaningful_new_queries(self) -> None:
        queries = generated_query_plan(sample_context())
        values = [item["query"] for item in queries]
        self.assertEqual("creative studio example city", values[0])
        self.assertIn("Creative workshop Example City", values)
        self.assertIn("Creative services Example City", values)
        self.assertIn("Known Studio", values)

    def test_search_stops_after_two_low_growth_queries_and_uses_count_1000(self) -> None:
        calls: list[dict] = []
        responses = {
            "q1": [{"id": value} for value in range(1, 101)],
            "q2": [{"id": value} for value in range(101, 105)],
            "q3": [{"id": value} for value in range(105, 109)],
            "q4": [{"id": 999}],
        }

        def fake_call(method: str, params: dict) -> dict:
            self.assertEqual("groups.search", method)
            self.assertEqual(1000, params["count"])
            calls.append(params)
            items = responses[params["q"]] if params["offset"] == 0 else []
            return {"count": len(items), "items": items}

        result, audit = discover_with_saturation(
            fake_call,
            [{"query": query, "origin": "test"} for query in responses],
            pool_target=1000,
        )
        self.assertEqual(108, len(result))
        self.assertEqual(3, len(calls))
        self.assertEqual(
            "two_consecutive_queries_below_five_percent",
            audit["stop_reason"],
        )

    def test_search_stops_at_three_candidates_per_target(self) -> None:
        calls = 0

        def fake_call(_method: str, params: dict) -> dict:
            nonlocal calls
            calls += 1
            return {
                "count": 30,
                "items": [{"id": value} for value in range(1, 31)],
            }

        result, audit = discover_with_saturation(
            fake_call,
            [{"query": "q1", "origin": "test"}, {"query": "q2", "origin": "test"}],
            pool_target=30,
        )
        self.assertEqual(30, len(result))
        self.assertEqual(1, calls)
        self.assertEqual("pool_reached_three_per_target", audit["stop_reason"])

    def test_prefilter_applies_type_followers_and_geography(self) -> None:
        config = normalize_context(sample_context())
        items = [
            candidate(1001),
            candidate(1002, followers=10),
            candidate(1003, city="Other City"),
            candidate(1004, community_type="application"),
        ]
        processed, audit = prefilter_candidates(items, config)
        by_id = {item["community_id"]: item for item in audit}
        self.assertTrue(by_id[1001]["passed"])
        self.assertEqual("followers_out_of_range", by_id[1002]["code"])
        self.assertEqual("geography_mismatch", by_id[1003]["code"])
        self.assertEqual("unsupported_community_type", by_id[1004]["code"])
        self.assertEqual(4, len(processed))

    def test_semantic_batches_are_ranked_compact_and_capped_at_200(self) -> None:
        context = sample_context(target=200)
        config = normalize_context(context)
        items = []
        for value in range(1, 451):
            item = candidate(value)
            item["description"] = "x" * 5000
            item["required_inclusion"] = value == 450
            item["rank_score"] = 0
            items.append(item)
        manifest, batches = build_review_batches(
            context,
            items,
            business_context_sha256="a" * 64,
            raw_candidates_sha256="b" * 64,
            batch_size=200,
        )
        self.assertEqual([200, 200, 50], [len(batch["candidates"]) for batch in batches])
        self.assertEqual(450, batches[0]["candidates"][0]["community_id"])
        self.assertLessEqual(len(batches[0]["candidates"][1]["description"]), 700)
        self.assertEqual(3, manifest["total_batches"])
        self.assertEqual(600, config["pool_target"])

    def test_review_requires_primary_sources_majority_and_key_features(self) -> None:
        context = sample_context()
        config = normalize_context(context)
        raw_candidate = candidate()
        reviews = reviews_document([valid_review()])
        self.assertEqual(
            [],
            validate_review_document(
                reviews,
                candidates_by_id={1001: raw_candidate},
                config=config,
            ),
        )
        bad = deepcopy(reviews)
        bad["candidates"][0]["classification_evidence"][
            "primary_offer_coverage_ratio"
        ] = 0.5
        bad["candidates"][0]["key_feature_check"]["matches"] = False
        bad["candidates"][0]["source_checks"] = []
        errors = validate_review_document(
            bad,
            candidates_by_id={1001: raw_candidate},
            config=config,
        )
        self.assertTrue(any("majority" in error for error in errors), errors)
        self.assertTrue(any("key-feature" in error for error in errors), errors)
        self.assertTrue(any("official VK" in error for error in errors), errors)

    def test_explicit_exclusion_blocks_inclusion(self) -> None:
        review = valid_review()
        review["constraint_check"]["matched_exclusions"] = ["Franchises"]
        errors = validate_review_document(
            reviews_document([review]),
            candidates_by_id={1001: candidate()},
            config=normalize_context(sample_context()),
        )
        self.assertTrue(any("explicit exclusion" in error for error in errors))

    def test_measure_calls_wall_only_for_semantically_included_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context_path = root / "context.json"
            raw_path = root / "raw.json"
            reviews_path = root / "reviews.json"
            context = sample_context(target=1)
            write_json(context_path, context)
            items = [candidate(1001), candidate(1002)]
            raw = raw_document(sha256_file(context_path), items)
            write_json(raw_path, raw)
            review_items = [
                valid_review(1001),
                valid_review(1002, decision="exclude", competitor_type=None),
            ]
            reviews = reviews_document(review_items)
            write_json(reviews_path, reviews)
            calls: list[int] = []

            def fake_call(method: str, params: dict) -> dict:
                self.assertEqual("wall.get", method)
                calls.append(params["owner_id"])
                return {"count": 1, "items": [make_post(10)]}

            measured = build_measured_candidates(
                context,
                raw,
                reviews,
                context_hash=sha256_file(context_path),
                raw_hash=sha256_file(raw_path),
                reviews_hash=sha256_file(reviews_path),
                call=fake_call,
                window_end=datetime(2026, 2, 1, tzinfo=timezone.utc),
                api_version="5.199",
                retry_policy={"attempts": 5},
            )
            self.assertEqual([-1001], calls)
            self.assertEqual(744, measured["metric_window"]["duration_hours"])
            self.assertEqual(
                "semantic_not_included",
                measured["candidates"][1]["wall"]["stop_reason"],
            )

    def test_old_pinned_post_does_not_stop_pagination(self) -> None:
        old = START_EPOCH - 86400
        fresh = END_EPOCH - 3600
        calls: list[int] = []
        pages = {
            0: [make_post(1, date=old, pinned=True), make_post(2, date=old, pinned=True)],
            2: [make_post(3, date=fresh), make_post(4, date=old)],
            4: [],
        }

        def fake_call(method: str, params: dict) -> dict:
            self.assertEqual("wall.get", method)
            self.assertEqual(2, params["count"])
            calls.append(params["offset"])
            return {"count": 4, "items": pages[params["offset"]]}

        result = collect_wall_posts(
            fake_call,
            1001,
            window_start_epoch=START_EPOCH,
            window_end_epoch=END_EPOCH,
            page_size=2,
        )
        self.assertEqual([0, 2, 4], calls)
        self.assertEqual([3], [item["id"] for item in result["posts"]])

    def test_candidate_and_post_duplicates_are_removed(self) -> None:
        candidates = dedupe_candidates(
            [
                {"id": 1001, "matched_queries": ["one"], "query_origins": ["a"]},
                {"id": 1001, "matched_queries": ["two"], "query_origins": ["b"]},
            ]
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual(["one", "two"], candidates[0]["matched_queries"])
        self.assertEqual(["a", "b"], candidates[0]["query_origins"])
        self.assertEqual(
            1,
            len(dedupe_posts([make_post(10), deepcopy(make_post(10))])),
        )

    def test_valid_result_has_all_outputs_and_synchronized_excel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(Path(temporary))
            self.assertTrue(fixture.finalize())
            self.assertEqual(
                [],
                validate_output_directory(fixture.output_dir, fixture.context_path),
            )
            result = load_json(fixture.output_dir / "competitor_set.json")
            self.assertEqual("draft", result["status"])
            self.assertEqual("Прямой", result["competitors"][0]["competitor_type"])
            self.assertEqual("direct", result["competitors"][0]["competitor_type_code"])
            self.assertEqual(744, result["window"]["duration_hours"])
            self.assertEqual(
                SHEET_NAMES,
                workbook_sheet_names(fixture.output_dir / "competitor_analysis.xlsx"),
            )
            rows = workbook_rows(fixture.output_dir / "competitor_analysis.xlsx")
            self.assertEqual("Example Competitor", rows["Конкуренты"][4][0])
            self.assertEqual(1001, rows["Посты за месяц"][4][0])

    def test_unconfirmed_business_context_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(
                Path(temporary),
                context=sample_context(status="draft"),
            )
            with self.assertRaisesRegex(PipelineError, "status: confirmed"):
                fixture.finalize()

    def test_zero_and_missing_views_have_explicit_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(
                Path(temporary),
                posts_by_id={
                    1001: [make_post(10, views=0), make_post(11, views=None)]
                },
            )
            fixture.finalize()
            result = load_json(fixture.output_dir / "competitor_set.json")
            reasons = [item["er_exclusion_reason"] for item in result["posts_in_window"]]
            self.assertEqual(["missing_views", "zero_views"], sorted(reasons))
            self.assertIsNone(result["competitors"][0]["average_er"])
            self.assertEqual(2, result["competitors"][0]["er_excluded_post_count"])
            self.assertEqual(2, len(result["er_exclusions"]))

    def test_shortfall_preserves_conditions_after_pool_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(
                Path(temporary),
                context=sample_context(target=2),
            )
            self.assertFalse(fixture.finalize())
            result = load_json(fixture.output_dir / "competitor_set.json")
            self.assertFalse(result["validation"]["target_met"])
            self.assertFalse(result["validation"]["conditions_relaxed"])
            self.assertEqual(1, result["criteria"]["actual_count"])

    def test_shortfall_is_blocked_while_an_unreviewed_candidate_remains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            items = [candidate(1001), candidate(1002)]
            fixture = PipelineFixture(
                Path(temporary),
                context=sample_context(target=2),
                candidates=items,
                reviews=[valid_review(1001)],
                posts_by_id={1001: [make_post(10)]},
            )
            with self.assertRaisesRegex(PipelineError, "more semantic review batches"):
                fixture.finalize()

    def test_broken_provenance_chain_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(Path(temporary))
            fixture.finalize()
            raw_path = fixture.output_dir / "raw_candidates.json"
            raw = load_json(raw_path)
            raw["limitations"].append("manual change")
            write_json(raw_path, raw)
            errors = validate_output_directory(
                fixture.output_dir,
                fixture.context_path,
            )
            self.assertTrue(
                any("raw_candidates.json hash mismatch" in error for error in errors),
                errors,
            )

    def test_secret_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(Path(temporary))
            reviews = load_json(fixture.reviews_path)
            reviews["candidates"][0]["reason"] += " vk1." + ("A" * 40)
            write_json(fixture.reviews_path, reviews)
            measured = load_json(fixture.measured_path)
            measured["provenance"]["candidate_reviews_sha256"] = sha256_file(
                fixture.reviews_path
            )
            write_json(fixture.measured_path, measured)
            with self.assertRaisesRegex(PipelineError, "potential secret"):
                fixture.finalize()

    def test_excel_content_tampering_is_detected_even_with_updated_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(Path(temporary))
            fixture.finalize()
            xlsx = fixture.output_dir / "competitor_analysis.xlsx"
            replacement = fixture.output_dir / "replacement.xlsx"
            with zipfile.ZipFile(xlsx) as source, zipfile.ZipFile(
                replacement, "w", zipfile.ZIP_DEFLATED
            ) as target:
                for name in source.namelist():
                    payload = source.read(name)
                    if name == "xl/worksheets/sheet2.xml":
                        payload = payload.replace(
                            b"Example Competitor",
                            b"Changed Competitor",
                        )
                    target.writestr(name, payload)
            xlsx.unlink()
            replacement.rename(xlsx)
            passport_path = fixture.output_dir / "run_passport.json"
            passport = load_json(passport_path)
            passport["output_hashes"]["competitor_analysis.xlsx"] = sha256_file(xlsx)
            write_json(passport_path, passport)
            errors = validate_output_directory(
                fixture.output_dir,
                fixture.context_path,
            )
            self.assertIn("Excel competitor data mismatch", errors)

    def test_confirmation_synchronizes_status_and_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = PipelineFixture(Path(temporary))
            fixture.finalize()
            confirm_outputs(fixture.output_dir, "2026-02-02")
            self.assertEqual(
                [],
                validate_output_directory(fixture.output_dir, fixture.context_path),
            )
            for name in (
                "competitor_set.json",
                "candidate_audit.json",
                "run_passport.json",
            ):
                document = load_json(fixture.output_dir / name)
                self.assertEqual("confirmed", document["status"])
                self.assertEqual("2026-02-02", document["confirmation_date"])

    def test_invalid_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(PipelineError, "invalid JSON"):
                load_json(path)


if __name__ == "__main__":
    unittest.main()
