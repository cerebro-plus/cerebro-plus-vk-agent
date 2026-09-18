from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import ssl
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import analyze_vk_best_posts as cli
from common import (
    InputValidationError,
    MediaGateError,
    VkApiError,
    load_json,
    scan_secret_files,
    write_json,
)
from media_pipeline import (
    direct_video_source,
    evaluate_media_gate,
    image_candidates,
    player_url_from_oembed,
    prepare_media_environment,
    transcribe_clip,
)
from metrics import (
    apply_benchmarks,
    calculate_er,
    classify_post,
    deduplicate_posts,
    giveaway_flag,
    verify_video_attachment,
)
from pipeline import (
    collect_week,
    create_json_outputs,
    deduplicate_normalized_posts,
    validate_outputs,
)
from schema_validator import validate_schema
from vk_api import VkClient, VkPreflightError
from xlsx_writer import EXPECTED_SHEETS, build_workbook


ONE_PIXEL_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "2wBDAf//////////////////////////////////////////////////////////////////////////////////////"
    "wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/"
    "9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/"
    "aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ/"
    "/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/"
    "9oACAEDAQE/EB//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EB//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/"
    "9oACAEBAAE/EB//2Q=="
)


def competitor_set() -> dict:
    return {
        "schema_version": "1.0",
        "status": "confirmed",
        "confirmed_at": "2026-01-07T12:00:00+03:00",
        "competitors": [
            {
                "community_id": 1,
                "name": "Example studio",
                "vk": "https://vk.com/example_studio",
                "competitor_type": "Прямой",
                "followers": 1000,
            }
        ],
    }


def dataset(source_hash: str, with_image: bool = False) -> dict:
    collage_path = "media/collages/m1_10.jpg" if with_image else None
    post = {
        "post_id": "-1_10",
        "owner_id": -1,
        "vk_post_id": 10,
        "community_id": 1,
        "community_name": "Example studio",
        "community_vk": "https://vk.com/example_studio",
        "competitor_type": "Прямой",
        "followers": 1000,
        "date": "2026-01-07T09:00:00Z",
        "timestamp": 1767776400,
        "url": "https://vk.com/wall-1_10",
        "text": "Example",
        "post_type": "Текстовый пост",
        "poll": False,
        "giveaway": False,
        "giveaway_evidence": [],
        "likes": 2,
        "comments": 0,
        "reposts": 0,
        "views": 10,
        "er": 0.2,
        "er_status": "calculated",
        "benchmark": 0.2,
        "is_best": True,
        "result": "Лучший пост",
        "is_pinned": False,
        "attachments": [],
        "verified_videos": [],
        "media": {
            "images": [],
            "collage_path": collage_path,
            "collage_width": 1 if with_image else None,
            "collage_height": 1 if with_image else None,
            "clip_path": None,
            "transcription_status": None,
            "transcript": None,
        },
        "data_status": "complete",
    }
    return {
        "schema_version": "1.0",
        "status": "draft",
        "run_started_at": "2026-01-07T12:00:00+03:00",
        "timezone": "Europe/Moscow",
        "window": {
            "start": "2025-12-31T09:00:00Z",
            "end": "2026-01-07T09:00:00Z",
            "duration_hours": 168,
            "inclusion": "start <= post.date <= end",
        },
        "source": {
            "competitor_set_path": "competitor_set.json",
            "competitor_set_sha256": source_hash,
            "competitor_set_status": "confirmed",
            "competitor_count": 1,
            "new_competitor_search_performed": False,
        },
        "communities": competitor_set()["competitors"],
        "posts": [post],
        "unavailable": [],
        "benchmarks": {"1": {"benchmark": 0.2, "valid_post_count": 1}},
        "pagination": {},
        "api_audit": {"calls": 2, "retries": 0, "errors": []},
        "media_audit": {
            "expected_images": 1 if with_image else 0,
            "downloaded_images": 1 if with_image else 0,
            "expected_collages": 1 if with_image else 0,
            "created_collages": 1 if with_image else 0,
            "confirmed_clips": 0,
            "downloaded_clips": 0,
            "clip_transcriptions": [],
            "media_paths": [collage_path] if collage_path else [],
            "gate": {
                "passed": True,
                "degraded": False,
                "degraded_consent_recorded": False,
                "reasons": [],
            },
        },
        "validation": {"completed": False},
    }


def prepare_bundle(run_dir: Path, with_image: bool = False) -> Path:
    source = run_dir / "competitor_set.json"
    write_json(source, competitor_set())
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    write_json(
        run_dir / "preflight.json",
        {
            "started_at": "2026-01-07T12:00:00+03:00",
            "timezone": "Europe/Moscow",
            "competitor_set": source.name,
            "competitor_set_sha256": source_hash,
        },
    )
    value = dataset(source_hash, with_image=with_image)
    write_json(run_dir / "raw" / "vk_posts_raw.json", value)
    write_json(
        run_dir / "raw" / "vk_comments_raw.json",
        {
            "-1_10": {
                "post_id": "-1_10",
                "post_url": "https://vk.com/wall-1_10",
                "api_post_comment_count": 0,
                "top_level_comments": [],
                "audit": {},
            }
        },
    )
    if with_image:
        collage = run_dir / "media" / "collages" / "m1_10.jpg"
        collage.parent.mkdir(parents=True, exist_ok=True)
        collage.write_bytes(ONE_PIXEL_JPEG)
    create_json_outputs(run_dir)
    return source


class AnalyzeVkBestPostsTests(unittest.TestCase):
    def test_preflight_preserves_one_moscow_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            source = run_dir / "competitor_set.json"
            token = run_dir / "token.txt"
            write_json(source, competitor_set())
            token.write_text("test-token", encoding="utf-8")
            args = argparse.Namespace(
                competitor_set=source,
                run_dir=run_dir,
                node=Path("node.exe"),
                node_modules=Path("node_modules"),
                started_at="2026-01-07T12:00:00+03:00",
                token_file=token,
            )
            api_client = mock.Mock()
            api_client.preflight.return_value = {"status": "passed", "methods": ["wall.get"], "api_calls": 1}
            with (
                mock.patch.object(cli, "prepare_media_environment", return_value={}),
                mock.patch.object(cli, "check_xlsx_environment", return_value={}),
                mock.patch.object(cli, "prepare_node_modules"),
                mock.patch.object(cli, "VkClient", return_value=api_client),
            ):
                cli.preflight(args)
                args.started_at = None
                cli.preflight(args)
                self.assertEqual(
                    load_json(run_dir / "preflight.json")["started_at"],
                    "2026-01-07T12:00:00+03:00",
                )
                args.started_at = "2026-01-07T12:01:00+03:00"
                with self.assertRaises(InputValidationError):
                    cli.preflight(args)

    def test_collect_reuses_preflight_start_and_rejects_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            source = run_dir / "competitor_set.json"
            token = run_dir / "token.txt"
            write_json(source, competitor_set())
            token.write_text("test-token", encoding="utf-8")
            write_json(
                run_dir / "preflight.json",
                {
                    "started_at": "2026-01-07T12:00:00+03:00",
                    "competitor_set_sha256": hashlib.sha256(
                        source.read_bytes()
                    ).hexdigest(),
                },
            )
            args = argparse.Namespace(
                competitor_set=source,
                run_dir=run_dir,
                token_file=token,
                started_at=None,
            )
            with (
                mock.patch.object(cli, "VkClient"),
                mock.patch.object(cli, "collect_week") as collect_week,
            ):
                cli.collect(args)
                self.assertEqual(
                    collect_week.call_args.args[3],
                    "2026-01-07T12:00:00+03:00",
                )
            args.started_at = "2026-01-07T12:00:01+03:00"
            with self.assertRaises(InputValidationError):
                cli.collect(args)

    def test_old_pinned_post_does_not_stop_pagination(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.offsets = []

            def call(self, _method, params):
                self.offsets.append(params["offset"])
                if params["offset"] == 0:
                    return {
                        "count": 3,
                        "items": [
                            {"owner_id": -1, "id": 1, "date": 10, "is_pinned": 1},
                            {"owner_id": -1, "id": 2, "date": 150},
                        ],
                    }
                return {
                    "count": 3,
                    "items": [{"owner_id": -1, "id": 3, "date": 90}],
                }

        fake = FakeClient()
        posts, audit = VkClient.wall_window(fake, 1, 100, 200)
        self.assertEqual(fake.offsets, [0, 2])
        self.assertEqual([item["id"] for item in posts], [2])
        self.assertEqual(audit["old_pinned_skipped"], 1)

    def test_comments_and_replies_are_fully_paginated(self) -> None:
        class FakeClient:
            def call(self, _method, params):
                if params.get("comment_id"):
                    return {
                        "items": [
                            {"id": 111, "text": "r11"},
                            {"id": 112, "text": "r12"},
                        ]
                    }
                return {
                    "count": 1,
                    "items": [
                        {
                            "id": 10,
                            "text": "top",
                            "thread": {
                                "count": 12,
                                "items": [
                                    {"id": value, "text": f"r{value}"}
                                    for value in range(101, 111)
                                ],
                            },
                        }
                    ],
                }

        comments, audit = VkClient.all_comments(FakeClient(), -1, 10)
        self.assertEqual(len(comments), 1)
        self.assertEqual(len(comments[0]["_collected_replies"]), 12)
        self.assertEqual(audit["replies_collected"], 12)
        self.assertEqual(audit["reply_pages"], 1)

    def test_zero_views_have_null_er_and_do_not_affect_benchmark(self) -> None:
        self.assertIsNone(calculate_er(10, 2, 1, 0))
        posts = [
            {"community_id": 1, "er": None},
            {"community_id": 1, "er": 0.1},
            {"community_id": 1, "er": 0.3},
        ]
        benchmark = apply_benchmarks(posts)["1"]["benchmark"]
        self.assertAlmostEqual(benchmark, 0.2)
        self.assertFalse(posts[0]["is_best"])
        self.assertEqual(posts[0]["result"], "ER не рассчитан")

    def test_duplicate_posts_are_removed(self) -> None:
        posts = [
            {"owner_id": -1, "id": 10},
            {"owner_id": -1, "id": 10},
            {"owner_id": -1, "id": 11},
        ]
        unique, duplicates = deduplicate_posts(posts)
        self.assertEqual(len(unique), 2)
        self.assertEqual(duplicates, 1)

    def test_unconfirmed_outer_video_is_not_classified_as_video(self) -> None:
        attachment = {"type": "video", "video": {"owner_id": -1, "id": 2}}
        verified = verify_video_attachment(attachment, resolver=lambda _value: None)
        self.assertEqual(verified["verification_status"], "unavailable")
        with self.assertRaises(InputValidationError):
            classify_post([attachment], [verified])

    def test_cross_community_duplicate_prefers_owning_community(self) -> None:
        shared = {
            "post_id": "-20_30",
            "owner_id": -20,
            "community_id": 10,
        }
        owned = {
            "post_id": "-20_30",
            "owner_id": -20,
            "community_id": 20,
        }
        unique, duplicates = deduplicate_normalized_posts([shared, owned])
        self.assertEqual(duplicates, 1)
        self.assertEqual(unique, [owned])

    def test_inner_or_outer_clip_and_confirmed_video_types(self) -> None:
        outer_clip = {"type": "clip", "video": {"owner_id": -1, "id": 2}}
        inner_clip = {
            "type": "video",
            "video": {"owner_id": -1, "id": 3, "type": "short_video"},
        }
        ordinary = {"type": "video", "video": {"owner_id": -1, "id": 4}}
        outer_short = {
            "type": "short_video",
            "short_video": {"owner_id": -1, "id": 5},
        }
        verified_outer = verify_video_attachment(outer_clip)
        verified_inner = verify_video_attachment(inner_clip)
        verified_short = verify_video_attachment(outer_short)
        verified_video = verify_video_attachment(
            ordinary, resolver=lambda _value: {"type": "video"}
        )
        self.assertEqual(classify_post([outer_clip], [verified_outer]), "Клип")
        self.assertEqual(classify_post([inner_clip], [verified_inner]), "Клип")
        self.assertEqual(classify_post([outer_short], [verified_short]), "Клип")
        self.assertEqual(verified_short["video_id"], 5)
        self.assertEqual(classify_post([ordinary], [verified_video]), "Видео")

    def test_poll_only_post_is_not_forced_into_text_type(self) -> None:
        with self.assertRaises(InputValidationError):
            classify_post([{"type": "poll", "poll": {}}], [])

    def test_unclassifiable_post_is_saved_to_unavailable_audit(self) -> None:
        class FakeClient:
            calls = 1
            retries = 0
            errors = []

            def wall_window(self, _community_id, _start_ts, _end_ts):
                return (
                    [
                        {
                            "owner_id": -1,
                            "id": 10,
                            "date": 1767776400,
                            "text": "Poll",
                            "attachments": [{"type": "poll", "poll": {}}],
                            "likes": {"count": 0},
                            "comments": {"count": 0},
                            "reposts": {"count": 0},
                            "views": {"count": 10},
                        }
                    ],
                    {"pages": 1},
                )

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            source = run_dir / "competitor_set.json"
            write_json(source, competitor_set())
            value = collect_week(
                source,
                "unused",
                run_dir,
                "2026-01-07T12:00:00+03:00",
                client=FakeClient(),
            )
            self.assertEqual(value["posts"], [])
            self.assertEqual(len(value["unavailable"]), 1)
            self.assertEqual(value["unavailable"][0]["scope"], "post_classification")

    def test_structured_carousel_images_are_extracted_and_deduplicated(self) -> None:
        post = {
            "post_type": "Карусель",
            "attachments": [
                {
                    "type": "pretty_cards",
                    "pretty_cards": {
                        "cards": [
                            {
                                "images": [
                                    {
                                        "url": "https://example.test/a-800.jpg",
                                        "width": 800,
                                        "height": 800,
                                    },
                                    {
                                        "url": "https://example.test/a-1600.jpg",
                                        "width": 1600,
                                        "height": 1600,
                                    },
                                ]
                            },
                            {
                                "images": [
                                    {
                                        "url": "https://example.test/b.jpg",
                                        "width": 1200,
                                        "height": 900,
                                    }
                                ]
                            },
                        ]
                    },
                }
            ],
            "verified_videos": [],
        }
        candidates = image_candidates(post)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["url"], "https://example.test/a-1600.jpg")

    def test_giveaway_requires_active_mechanics(self) -> None:
        positive, evidence = giveaway_flag(
            "Розыгрыш сертификата! Подпишитесь и оставьте комментарий до 10.08."
        )
        historical, _ = giveaway_flag(
            "Итоги конкурса: победитель получил подарок."
        )
        self.assertTrue(positive)
        self.assertGreaterEqual(len(evidence), 2)
        self.assertFalse(historical)

    def test_video_sources_and_oembed_parser(self) -> None:
        url, kind = direct_video_source(
            {"mp4_360": "low.mp4", "mp4_1080": "high.mp4", "hls": "stream.m3u8"}
        )
        self.assertEqual((url, kind), ("high.mp4", "mp4"))
        self.assertEqual(
            player_url_from_oembed({"html": '<iframe src="https://vk.com/player"></iframe>'}),
            "https://vk.com/player",
        )

    def test_whisper_small_with_vad_and_no_speech(self) -> None:
        captured = {}

        class FakeModel:
            def transcribe(self, _path, **kwargs):
                captured.update(kwargs)
                return iter([]), object()

        def factory(model_name, **kwargs):
            captured["model_name"] = model_name
            captured.update(kwargs)
            return FakeModel()

        result = transcribe_clip(Path("clip.mp4"), Path("models"), model_factory=factory)
        self.assertEqual(result["status"], "no_speech")
        self.assertEqual(captured["model_name"], "small")
        self.assertTrue(captured["vad_filter"])

    def test_missing_media_dependencies_block_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(MediaGateError):
                prepare_media_environment(
                    Path(temp),
                    module_lookup=lambda _name: None,
                    executable_lookup=lambda _name: None,
                )

    def test_tls_error_is_retried_and_audited_without_token(self) -> None:
        def opener(_request, timeout):
            raise urllib.error.URLError(ssl.SSLError("certificate failure"))

        client = VkClient(
            "token-value",
            opener=opener,
            sleeper=lambda _seconds: None,
            max_attempts=2,
        )
        with self.assertRaises(VkApiError):
            client.call("wall.get", {"owner_id": -1})
        self.assertEqual(client.retries, 1)
        self.assertTrue(client.errors[0]["tls_error"])
        self.assertNotIn("token-value", json.dumps(client.errors))

    def test_vk_preflight_classifies_permission_error_without_retries(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "error": {"error_code": 15, "error_msg": "Access denied"}
                }).encode("utf-8")

        client = VkClient(
            "token-value",
            opener=lambda _request, timeout: Response(),
            sleeper=lambda _seconds: None,
            max_attempts=4,
        )
        with self.assertRaises(VkPreflightError) as captured:
            client.preflight([("wall.get", {"owner_id": -1, "count": 1})])
        self.assertEqual(captured.exception.reason_code, "insufficient_permissions")
        self.assertEqual(client.retries, 0)

    def test_competitor_search_is_forbidden(self) -> None:
        client = VkClient("token", sleeper=lambda _seconds: None)
        with self.assertRaises(VkApiError):
            client.call("groups.search", {"q": "test"})

    def test_zero_downloaded_images_and_clips_are_blocked(self) -> None:
        with self.assertRaises(MediaGateError):
            evaluate_media_gate(
                {
                    "expected_images": 3,
                    "downloaded_images": 0,
                    "expected_collages": 1,
                    "created_collages": 0,
                    "confirmed_clips": 2,
                    "downloaded_clips": 0,
                    "clip_transcriptions": [],
                    "media_paths": [],
                }
            )

    def test_missing_best_post_collage_is_blocked(self) -> None:
        with self.assertRaises(MediaGateError):
            evaluate_media_gate(
                {
                    "expected_images": 2,
                    "downloaded_images": 2,
                    "expected_collages": 1,
                    "created_collages": 0,
                    "confirmed_clips": 0,
                    "downloaded_clips": 0,
                    "clip_transcriptions": [],
                    "media_paths": [],
                }
            )

    def test_unprocessed_clip_is_blocked(self) -> None:
        with self.assertRaises(MediaGateError):
            evaluate_media_gate(
                {
                    "expected_images": 0,
                    "downloaded_images": 0,
                    "expected_collages": 0,
                    "created_collages": 0,
                    "confirmed_clips": 1,
                    "downloaded_clips": 1,
                    "clip_transcriptions": [
                        {"post_id": "-1_10", "status": "unavailable"}
                    ],
                    "media_paths": [],
                }
            )

    def test_broken_media_path_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(MediaGateError):
                evaluate_media_gate(
                    {
                        "expected_images": 1,
                        "downloaded_images": 1,
                        "expected_collages": 0,
                        "created_collages": 0,
                        "confirmed_clips": 0,
                        "downloaded_clips": 0,
                        "clip_transcriptions": [],
                        "media_paths": [str(Path(temp) / "missing.jpg")],
                    }
                )

    def test_degraded_run_requires_explicit_consent(self) -> None:
        summary = {
            "expected_images": 1,
            "downloaded_images": 0,
            "expected_collages": 1,
            "created_collages": 0,
            "confirmed_clips": 0,
            "downloaded_clips": 0,
            "clip_transcriptions": [],
            "media_paths": [],
        }
        with self.assertRaises(MediaGateError):
            evaluate_media_gate(summary, allow_degraded=True, degraded_consent="")
        result = evaluate_media_gate(
            summary,
            allow_degraded=True,
            degraded_consent="Пользователь явно согласовал деградированный результат.",
        )
        self.assertTrue(result["degraded"])
        self.assertTrue(result["degraded_consent_recorded"])

    def test_corrupted_json_and_schema_violation_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            broken = Path(temp) / "broken.json"
            broken.write_text("{", encoding="utf-8")
            with self.assertRaises(InputValidationError):
                load_json(broken)
        invalid = competitor_set()
        invalid.pop("status")
        with self.assertRaises(InputValidationError):
            validate_schema(
                invalid,
                SKILL_ROOT / "references" / "competitor-set.schema.json",
                "competitor_set",
            )

    def test_secret_scan_detects_literal_and_vk_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output.json"
            token = "vk1." + "A" * 24
            path.write_text(json.dumps({"value": token}), encoding="utf-8")
            hits = scan_secret_files([path], literal_secret=token)
            self.assertTrue(any(item["pattern"] == "vk_token" for item in hits))
            self.assertTrue(any(item["pattern"] == "literal_secret" for item in hits))

    @unittest.skipUnless(
        os.environ.get("NODE_BINARY") and os.environ.get("NODE_MODULES"),
        "Set NODE_BINARY and NODE_MODULES for XLSX integration",
    )
    def test_real_xlsx_bundle_passes_deep_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            source = prepare_bundle(run_dir, with_image=True)
            report = build_workbook(
                run_dir,
                Path(os.environ["NODE_BINARY"]),
                Path(os.environ["NODE_MODULES"]),
                SKILL_ROOT / "scripts" / "build_workbook.mjs",
            )
            self.assertTrue(report["deep_validation"]["passed"])
            self.assertEqual(report["deep_validation"]["embedded_images"], 1)
            validation = validate_outputs(source, run_dir)
            self.assertTrue(validation["passed"])
            receipt = load_json(run_dir / "outputs" / "final_validation.json")
            self.assertTrue(receipt["passed"])
            self.assertEqual(receipt["counts"]["embedded_images"], 1)

    @unittest.skipUnless(
        os.environ.get("NODE_BINARY") and os.environ.get("NODE_MODULES"),
        "Set NODE_BINARY and NODE_MODULES for XLSX integration",
    )
    def test_zero_post_window_still_builds_valid_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            source = run_dir / "competitor_set.json"
            write_json(source, competitor_set())
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            write_json(
                run_dir / "preflight.json",
                {
                    "started_at": "2026-01-07T12:00:00+03:00",
                    "timezone": "Europe/Moscow",
                    "competitor_set_sha256": source_hash,
                },
            )
            value = dataset(source_hash)
            value["posts"] = []
            value["benchmarks"] = {}
            value["media_audit"] = {
                "expected_images": 0,
                "downloaded_images": 0,
                "expected_collages": 0,
                "created_collages": 0,
                "confirmed_clips": 0,
                "downloaded_clips": 0,
                "clip_transcriptions": [],
                "media_paths": [],
                "gate": {
                    "passed": True,
                    "degraded": False,
                    "degraded_consent_recorded": False,
                    "reasons": [],
                },
            }
            write_json(run_dir / "raw" / "vk_posts_raw.json", value)
            write_json(run_dir / "raw" / "vk_comments_raw.json", {})
            create_json_outputs(run_dir)
            report = build_workbook(
                run_dir,
                Path(os.environ["NODE_BINARY"]),
                Path(os.environ["NODE_MODULES"]),
                SKILL_ROOT / "scripts" / "build_workbook.mjs",
            )
            self.assertEqual(report["deep_validation"]["checked_rows"], 0)
            self.assertTrue(validate_outputs(source, run_dir)["passed"])

    def test_fake_pk_xlsx_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            source = prepare_bundle(run_dir)
            output = run_dir / "outputs"
            (output / "vk_best_posts.xlsx").write_bytes(b"PK\x03\x04")
            write_json(
                output / "workbook_validation.json",
                {
                    "sheets": EXPECTED_SHEETS,
                    "best_columns": [],
                    "checked_columns": [],
                    "unavailable_columns": [],
                    "deep_validation": {"passed": True},
                },
            )
            passport_path = output / "run_passport.json"
            passport = load_json(passport_path)
            passport["hashes"]["vk_best_posts.xlsx"] = hashlib.sha256(
                (output / "vk_best_posts.xlsx").read_bytes()
            ).hexdigest()
            passport["hashes"]["workbook_validation.json"] = hashlib.sha256(
                (output / "workbook_validation.json").read_bytes()
            ).hexdigest()
            write_json(passport_path, passport)
            with self.assertRaises(InputValidationError):
                validate_outputs(source, run_dir)


if __name__ == "__main__":
    unittest.main()
