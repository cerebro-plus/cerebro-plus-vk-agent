from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pipeline_chain.py"
SPEC = importlib.util.spec_from_file_location("pipeline_chain", SCRIPT)
chain = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(chain)


def write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")
    return path


class ChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache = self.root / "hash_cache.json"
        self.business = write(
            self.root / "business_context.json",
            {
                "schema_version": "1.0",
                "status": "confirmed",
                "online_presence": {"vk_url": "https://vk.com/example"},
                "competitors": {
                    "search_constraints": {
                        "target_count": 2,
                        "geography": "Example City",
                        "audience_size": {"min": 1, "max": 1000},
                        "freshness_days": 31,
                    },
                    "proposed_search_queries": ["example query"],
                },
            },
        )
        business_sha, _ = chain.hash_file(self.business, self.cache)
        self.competitors = write(
            self.root / "competitor_set.json",
            {
                "schema_version": "1.0",
                "status": "confirmed",
                "provenance": {"business_context_sha256": business_sha},
                "competitors": [
                    {
                        "community_id": 1,
                        "name": "Example One",
                        "competitor_type": "Косвенный",
                        "followers": 10,
                    }
                ],
            },
        )
        competitor_sha, _ = chain.hash_file(self.competitors, self.cache)
        self.dataset = write(
            self.root / "vk_posts_dataset.json",
            {
                "schema_version": "1.0",
                "status": "confirmed",
                "source": {"competitor_set_sha256": competitor_sha},
                "communities": [{"community_id": 1}],
                "posts": [{"post_id": "-1_1"}],
                "window": {"duration_hours": 168},
            },
        )
        self.comments = write(
            self.root / "vk_comments.json",
            {"-1_1": {"post_id": "-1_1"}},
        )
        self.xlsx = write(self.root / "vk_best_posts.xlsx", "xlsx")
        dataset_sha, _ = chain.hash_file(self.dataset, self.cache)
        comments_sha, _ = chain.hash_file(self.comments, self.cache)
        xlsx_sha, _ = chain.hash_file(self.xlsx, self.cache)
        self.receipt = write(
            self.root / "final_validation.json",
            {
                "passed": True,
                "status": "confirmed",
                "hashes": {
                    "dataset": dataset_sha,
                    "comments": comments_sha,
                    "xlsx": xlsx_sha,
                },
            },
        )
        self.analysis = write(self.root / "analysis_input.json", {"status": "confirmed"})
        self.signals = write(self.root / "comment-signals.json", {"status": "confirmed"})
        self.findings = write(self.root / "report_findings.json", {"status": "confirmed"})
        self.html = write(self.root / "report.html", "<html><body>ok</body></html>")
        self.report_validation = write(
            self.root / "validation_report.json",
            {
                "passed": True,
                "status": "confirmed",
                "source_hashes": {
                    "dataset": dataset_sha,
                    "comments": comments_sha,
                    "xlsx": xlsx_sha,
                },
                "counts": {
                    "posts": 1,
                    "sections": 6,
                    "findings": 6,
                    "evidence_blocks": 6,
                    "visible_words": 500,
                },
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def emit(self, stage: str, primary: Path, output: Path, **extra):
        values = {
            "stage": stage,
            "primary": primary,
            "output": output,
            "cache": self.cache,
            "comments": None,
            "xlsx": None,
            "receipt": None,
            "analysis_input": None,
            "comment_signals": None,
            "findings": None,
            "html": None,
        }
        values.update(extra)
        return chain.emit(Namespace(**values))

    def build_chain(self):
        business_h = self.root / "business_handoff.json"
        competitor_h = self.root / "competitor_handoff.json"
        posts_h = self.root / "posts_handoff.json"
        report_h = self.root / "report_handoff.json"
        self.emit("business-context", self.business, business_h)
        self.emit("competitors", self.competitors, competitor_h)
        self.emit(
            "best-posts",
            self.dataset,
            posts_h,
            comments=self.comments,
            xlsx=self.xlsx,
            receipt=self.receipt,
        )
        self.emit(
            "market-reaction",
            self.report_validation,
            report_h,
            analysis_input=self.analysis,
            comment_signals=self.signals,
            findings=self.findings,
            html=self.html,
        )
        manifest = self.root / "chain_manifest.json"
        chain.assemble(
            Namespace(
                business=business_h,
                competitors=competitor_h,
                posts=posts_h,
                report=report_h,
                output=manifest,
                cache=self.cache,
                allow_draft_report=False,
            )
        )
        return manifest

    def test_positive_chain_and_cache(self):
        manifest = self.build_chain()
        result = chain.validate_manifest(
            Namespace(
                manifest=manifest,
                output=self.root / "chain_validation.json",
                cache=self.cache,
                allow_draft_report=False,
            )
        )
        self.assertTrue(result["passed"])
        second_hash, cached = chain.hash_file(self.dataset, self.cache)
        self.assertTrue(cached)
        self.assertRegex(second_hash, r"^[a-f0-9]{64}$")

    def test_broken_parent_hash_is_rejected(self):
        competitor = json.loads(self.competitors.read_text(encoding="utf-8"))
        competitor["provenance"]["business_context_sha256"] = "0" * 64
        write(self.competitors, competitor)
        with self.assertRaisesRegex(chain.ChainError, "hash mismatch"):
            self.build_chain()

    def test_post_id_mismatch_is_rejected(self):
        write(self.comments, {"-1_2": {"post_id": "-1_2"}})
        with self.assertRaisesRegex(chain.ChainError, "post_id mismatch"):
            self.emit(
                "best-posts",
                self.dataset,
                self.root / "posts_handoff.json",
                comments=self.comments,
                xlsx=self.xlsx,
                receipt=self.receipt,
            )

    def test_changed_artifact_after_handoff_is_rejected(self):
        manifest = self.build_chain()
        write(self.html, "<html><body>changed</body></html>")
        with self.assertRaisesRegex(chain.ChainError, "artifact hash mismatch"):
            chain.validate_manifest(
                Namespace(
                    manifest=manifest,
                    output=None,
                    cache=self.cache,
                    allow_draft_report=False,
                )
            )

    def test_secret_is_rejected(self):
        value = json.loads(self.business.read_text(encoding="utf-8"))
        value["unsafe"] = "vk1." + "A" * 40
        write(self.business, value)
        with self.assertRaisesRegex(chain.ChainError, "secret detected"):
            self.emit(
                "business-context",
                self.business,
                self.root / "business_handoff.json",
            )

    def test_draft_report_requires_explicit_allowance(self):
        report = json.loads(self.report_validation.read_text(encoding="utf-8"))
        report["status"] = "draft"
        write(self.report_validation, report)
        business_h = self.root / "business_handoff.json"
        competitor_h = self.root / "competitor_handoff.json"
        posts_h = self.root / "posts_handoff.json"
        report_h = self.root / "report_handoff.json"
        self.emit("business-context", self.business, business_h)
        self.emit("competitors", self.competitors, competitor_h)
        self.emit(
            "best-posts",
            self.dataset,
            posts_h,
            comments=self.comments,
            xlsx=self.xlsx,
            receipt=self.receipt,
        )
        self.emit(
            "market-reaction",
            self.report_validation,
            report_h,
            analysis_input=self.analysis,
            comment_signals=self.signals,
            findings=self.findings,
            html=self.html,
        )
        args = Namespace(
            business=business_h,
            competitors=competitor_h,
            posts=posts_h,
            report=report_h,
            output=self.root / "chain_manifest.json",
            cache=self.cache,
            allow_draft_report=False,
        )
        with self.assertRaisesRegex(chain.ChainError, "not confirmed"):
            chain.assemble(args)

    def test_legacy_confirmed_handoffs_remain_compatible(self):
        business = json.loads(self.business.read_text(encoding="utf-8"))
        business["online_presence"] = [
            {"type": "VK", "url": "https://vk.ru/example", "active": True}
        ]
        constraints = business["competitors"]["search_constraints"]
        business["competitors"]["search_constraints"] = {
            "target_total": constraints["target_count"],
            "geography": {"center": "Example City", "radius_km": 10},
            "vk_followers": constraints["audience_size"],
            "freshness": {"period_months": 1},
        }
        write(self.business, business)
        business_sha, _ = chain.hash_file(self.business, self.cache)
        competitors = json.loads(self.competitors.read_text(encoding="utf-8"))
        competitors.pop("provenance")
        competitors["business_context"] = {"sha256": business_sha}
        write(self.competitors, competitors)
        business_h = self.root / "legacy_business_handoff.json"
        competitor_h = self.root / "legacy_competitor_handoff.json"
        business_result = self.emit(
            "business-context", self.business, business_h
        )
        competitor_result = self.emit(
            "competitors", self.competitors, competitor_h
        )
        self.assertEqual(business_result["summary"]["freshness_days"], 31)
        self.assertEqual(
            competitor_result["parent"]["sha256"],
            business_result["primary"]["sha256"],
        )

    def test_business_context_v11_handoff_is_supported(self):
        business = json.loads(self.business.read_text(encoding="utf-8"))
        business["schema_version"] = "1.1"
        write(self.business, business)
        result = self.emit(
            "business-context",
            self.business,
            self.root / "business_v11_handoff.json",
        )
        self.assertEqual("confirmed", result["status"])
        self.assertEqual("https://vk.com/example", result["summary"]["vk_url"])

    def test_new_handoffs_derive_and_propagate_project_identity(self):
        business = json.loads(self.business.read_text(encoding="utf-8"))
        business["business"] = {"name": "  Example   Studio  "}
        write(self.business, business)
        business_sha, _ = chain.hash_file(self.business, self.cache)
        competitors = json.loads(self.competitors.read_text(encoding="utf-8"))
        competitors["provenance"]["business_context_sha256"] = business_sha
        write(self.competitors, competitors)
        business_h = self.root / "identity_business_handoff.json"
        competitor_h = self.root / "identity_competitor_handoff.json"
        business_result = self.emit("business-context", self.business, business_h)
        competitor_result = self.emit(
            "competitors",
            self.competitors,
            competitor_h,
            upstream_handoff=business_h,
        )
        self.assertEqual(business_result["schema_version"], "1.1")
        self.assertEqual(
            business_result["project_identity"]["business_name_canonical"],
            "example studio",
        )
        self.assertEqual(competitor_result["project_identity"], business_result["project_identity"])

    def test_cross_business_identity_is_rejected_during_assembly(self):
        manifest = self.build_chain()
        paths = [
            self.root / "business_handoff.json",
            self.root / "competitor_handoff.json",
            self.root / "posts_handoff.json",
            self.root / "report_handoff.json",
        ]
        business_handoff = json.loads(paths[0].read_text(encoding="utf-8"))
        identity = {
            "project_id": "business-" + chain.hashlib.sha256(b"example studio").hexdigest()[:16],
            "business_name_canonical": "example studio",
            "business_context_sha256": business_handoff["primary"]["sha256"],
        }
        for path in paths:
            handoff = json.loads(path.read_text(encoding="utf-8"))
            handoff["schema_version"] = "1.1"
            handoff["project_identity"] = dict(identity)
            write(path, handoff)
        wrong = json.loads(paths[2].read_text(encoding="utf-8"))
        wrong["project_identity"] = {
            "project_id": "business-" + chain.hashlib.sha256(b"other studio").hexdigest()[:16],
            "business_name_canonical": "other studio",
            "business_context_sha256": "2" * 64,
        }
        write(paths[2], wrong)
        with self.assertRaisesRegex(chain.ChainError, "project identity mismatch"):
            chain.assemble(
                Namespace(
                    business=paths[0],
                    competitors=paths[1],
                    posts=paths[2],
                    report=paths[3],
                    output=manifest,
                    cache=self.cache,
                    allow_draft_report=False,
                )
            )


if __name__ == "__main__":
    unittest.main()
