from __future__ import annotations

import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workspace.py"
SPEC = importlib.util.spec_from_file_location("workspace_router", SCRIPT)
workspace = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(workspace)


SHA_BUSINESS = "1" * 64
SHA_COMPETITORS = "2" * 64
SHA_DATASET = "3" * 64
SHA_COMMENTS = "4" * 64
SHA_XLSX = "5" * 64


def state(
    *,
    business=True,
    competitors=True,
    best=False,
    report=None,
    competitor_hash=SHA_COMPETITORS,
):
    return {
        "business_context": (
            {
                "valid": True,
                "path": "runs/context/business_context.json",
                "sha256": SHA_BUSINESS,
            }
            if business
            else None
        ),
        "competitor_set": (
            {
                "valid": True,
                "path": "runs/competitors/competitor_set.json",
                "sha256": competitor_hash,
                "business_context_sha256": SHA_BUSINESS,
            }
            if competitors
            else None
        ),
        "best_posts": (
            {
                "valid": True,
                "dataset_path": "runs/posts/vk_posts_dataset.json",
                "comments_path": "runs/posts/vk_comments.json",
                "xlsx_path": "runs/posts/vk_best_posts.xlsx",
                "receipt_path": "runs/posts/final_validation.json",
                "dataset_sha256": SHA_DATASET,
                "comments_sha256": SHA_COMMENTS,
                "xlsx_sha256": SHA_XLSX,
                "competitor_set_sha256": SHA_COMPETITORS,
                "duration_hours": 168,
            }
            if best
            else None
        ),
        "market_report": report,
    }


class RoutingTests(unittest.TestCase):
    def test_missing_business_routes_only_to_interview(self):
        result = workspace.route(state(business=False), "weekly-report")
        self.assertEqual(
            [item["skill"] for item in result["steps"]],
            ["business-context-interview"],
        )
        self.assertEqual(result["block_reason"], "business_context_required")

    def test_valid_context_does_not_repeat_interview(self):
        result = workspace.route(state(best=False), "weekly-report")
        self.assertNotIn(
            "business-context-interview",
            [item["skill"] for item in result["steps"]],
        )

    def test_explicit_context_update_runs_interview(self):
        result = workspace.route(state(), "update-context")
        self.assertEqual(result["steps"][0]["skill"], "business-context-interview")

    def test_direct_competitor_update_runs_search(self):
        result = workspace.route(state(), "update-competitors")
        self.assertEqual(result["steps"][0]["skill"], "search-vk-competitors")
        self.assertTrue(result["steps"][0]["inputs"]["force_refresh"])

    def test_weekly_report_never_implies_search(self):
        result = workspace.route(state(competitors=False), "weekly-report")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["block_reason"], "missing_competitor_set")
        self.assertEqual(result["steps"], [])

    def test_weekly_default_routes_posts_then_report(self):
        result = workspace.route(
            state(best=False), "weekly-report", analysis_mode="first_complex"
        )
        self.assertEqual(result["period_hours"], 168)
        self.assertEqual(
            [item["skill"] for item in result["steps"]],
            ["analyze-vk-best-posts", "vk-content-report"],
        )
        self.assertEqual(result["steps"][0]["model"], "gpt-5.6-terra")
        self.assertEqual(result["steps"][1]["model"], "gpt-5.6-sol")
        self.assertEqual(result["steps"][1]["effort"], "high")

    def test_regular_market_analysis_uses_medium(self):
        result = workspace.route(state(best=True), "market-report")
        self.assertEqual(result["steps"][0]["effort"], "medium")

    def test_reuses_best_posts_with_same_hash(self):
        result = workspace.route(state(best=True), "weekly-report")
        self.assertEqual(result["reuse"], ["best_posts"])
        self.assertEqual(
            [item["skill"] for item in result["steps"]],
            ["vk-content-report"],
        )

    def test_hash_mismatch_prevents_reuse(self):
        value = state(best=True, competitor_hash="9" * 64)
        result = workspace.route(value, "weekly-report")
        self.assertEqual(
            [item["skill"] for item in result["steps"]],
            ["analyze-vk-best-posts", "vk-content-report"],
        )

    def test_confirmed_report_is_fully_reused(self):
        report = {
            "valid": True,
            "confirmed": True,
            "source_hashes": {
                "dataset": SHA_DATASET,
                "comments": SHA_COMMENTS,
                "xlsx": SHA_XLSX,
            },
        }
        result = workspace.route(
            state(best=True, report=report), "weekly-report"
        )
        self.assertEqual(result["status"], "reuse")
        self.assertEqual(result["steps"], [])

    def test_draft_report_is_presented_without_rerun(self):
        report = {
            "valid": True,
            "confirmed": False,
            "source_hashes": {
                "dataset": SHA_DATASET,
                "comments": SHA_COMMENTS,
                "xlsx": SHA_XLSX,
            },
        }
        result = workspace.route(
            state(best=True, report=report), "weekly-report"
        )
        self.assertEqual(result["status"], "awaiting_confirmation")
        self.assertEqual(result["steps"], [])

    def test_previous_failure_blocks_next_stage(self):
        result = workspace.route(
            state(best=False),
            "weekly-report",
            previous_stage_status="failed",
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["steps"], [])

    def test_degraded_media_requires_consent(self):
        result = workspace.route(
            state(best=False),
            "weekly-report",
            degraded_media=True,
            degraded_consent=False,
        )
        self.assertEqual(
            result["block_reason"], "degraded_media_requires_consent"
        )
        self.assertEqual(result["steps"], [])

    def test_run_directory_is_unique_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = workspace.create_run(
                root,
                "analyze-vk-best-posts",
                SHA_COMPETITORS,
                run_id="weekly-test",
                date="2026-07-29",
            )
            self.assertEqual(
                first["run_path"], "runs/2026-07-29/weekly-test"
            )
            with self.assertRaisesRegex(
                workspace.WorkspaceError, "already exists"
            ):
                workspace.create_run(
                    root,
                    "analyze-vk-best-posts",
                    SHA_COMPETITORS,
                    run_id="weekly-test",
                    date="2026-07-29",
                )

    def test_inspection_rejects_corrupted_business_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "runs" / "broken" / "business_context.json"
            context.parent.mkdir(parents=True)
            context.write_text("{broken", encoding="utf-8")
            result = workspace.inspect_workspace(root)
            self.assertIsNone(result["business_context"])
            self.assertIn(
                "invalid JSON",
                result["invalid_candidates"]["business_context"][0]["reason"],
            )

    def test_inspection_rejects_unconfirmed_business_context(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "runs" / "draft" / "business_context.json"
            context.parent.mkdir(parents=True)
            context.write_text(
                json.dumps({"schema_version": "1.0", "status": "draft"}),
                encoding="utf-8",
            )
            result = workspace.inspect_workspace(root)
            self.assertIsNone(result["business_context"])
            self.assertEqual(
                result["invalid_candidates"]["business_context"][0]["reason"],
                "business context is not confirmed",
            )

    def test_inspection_accepts_business_context_v11(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = root / "runs" / "v11" / "business_context.json"
            context.parent.mkdir(parents=True)
            context.write_text(
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "status": "confirmed",
                        "confirmation_date": "2026-07-31",
                    }
                ),
                encoding="utf-8",
            )
            result = workspace.inspect_workspace(root)
            self.assertEqual(
                "1.1",
                result["business_context"]["schema_version"],
            )

    def test_report_requires_verified_html_for_reuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outputs = root / "runs" / "report" / "outputs"
            outputs.mkdir(parents=True)
            html = outputs / "vk_content_report.html"
            html.write_text("<!doctype html><html><body>Report</body></html>", encoding="utf-8")
            html_sha = hashlib.sha256(html.read_bytes()).hexdigest()
            validation = outputs / "validation_report.json"
            validation.write_text(json.dumps({
                "passed": True,
                "status": "confirmed",
                "source_hashes": {
                    "dataset": SHA_DATASET,
                    "comments": SHA_COMMENTS,
                    "xlsx": SHA_XLSX,
                },
                "output_hashes": {"html": html_sha},
            }), encoding="utf-8")
            valid = workspace.inspect_workspace(root)
            self.assertEqual(
                "runs/report/outputs/vk_content_report.html",
                valid["market_report"]["html_path"],
            )

            html.unlink()
            missing = workspace.inspect_workspace(root)
            self.assertIsNone(missing["market_report"])
            self.assertIn("HTML", missing["invalid_candidates"]["market_report"][0]["reason"])

            html.write_text("<html>Changed</html>", encoding="utf-8")
            altered = workspace.inspect_workspace(root)
            self.assertIsNone(altered["market_report"])
            self.assertIn("HTML", altered["invalid_candidates"]["market_report"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
