from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import source_intake as si  # noqa: E402


TOKEN = "vk1." + ("A" * 40)


class SourceIntakeTests(unittest.TestCase):
    def test_token_is_saved_without_echo_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = si.save_token(root, TOKEN)
            self.assertEqual(TOKEN, path.read_text(encoding="utf-8").strip())
            self.assertIn(
                "/vk_api_token.txt",
                (root / ".gitignore").read_text(encoding="utf-8"),
            )
            with self.assertRaisesRegex(si.SourceError, "already exists"):
                si.save_token(root, TOKEN)
            replacement = "vk1." + ("B" * 40)
            si.save_token(root, replacement, force=True)
            self.assertEqual(
                replacement,
                path.read_text(encoding="utf-8").strip(),
            )

    def test_token_path_cannot_escape_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(si.SourceError, "inside the project"):
                si.save_token(root, TOKEN, "../outside.txt")

    def test_vk_collection_uses_api_and_redacts_token(self) -> None:
        calls: list[tuple[str, dict[str, list[str]]]] = []

        def requester(url: str, body: bytes, timeout: float):
            params = parse_qs(body.decode("utf-8"))
            calls.append((url, params))
            if url.endswith("/groups.getById"):
                return {
                    "response": {
                        "groups": [
                            {
                                "id": 123,
                                "name": "Test",
                                "screen_name": "test",
                                "description": "Business description",
                                "members_count": 100,
                            }
                        ]
                    }
                }
            if url.endswith("/wall.get"):
                return {
                    "response": {
                        "items": [
                            {
                                "id": 1,
                                "date": 1,
                                "text": "Public post",
                                "likes": {"count": 2},
                            }
                        ]
                    }
                }
            if url.endswith("/market.get"):
                return {
                    "error": {
                        "error_code": 15,
                        "error_msg": "Access denied",
                        "request_params": [
                            {"key": "access_token", "value": TOKEN}
                        ],
                    }
                }
            if url.endswith("/groups.getAddresses"):
                return {
                    "response": {
                        "items": [
                            {"id": 1, "address": "Test address"}
                        ]
                    }
                }
            raise AssertionError(url)

        entries = si.normalize_source_list(
            {"sources": [{"label": "VK", "url": "https://vk.com/test"}]}
        )
        result = si.collect_sources(entries, TOKEN, requester=requester)
        self.assertEqual("partial", result["status"])
        self.assertNotIn(TOKEN, si.canonical_json(result))
        self.assertEqual([], si.validate_source_review(result))
        self.assertTrue(all(call[1]["v"] == ["5.199"] for call in calls))
        self.assertTrue(all(call[1]["access_token"] == [TOKEN] for call in calls))
        limitations = result["sources"][0]["limitations"]
        self.assertTrue(any("market.get unavailable" in item for item in limitations))

    def test_multiple_web_sources_are_collected(self) -> None:
        def fetcher(url: str, timeout: float):
            return {
                "final_url": url,
                "content_type": "text/html; charset=utf-8",
                "text": (
                    "<html><head><title>Test</title>"
                    "<meta name='description' content='Description'></head>"
                    "<body><main>Visible business information</main></body></html>"
                ),
                "truncated": False,
            }

        entries = si.normalize_source_list(
            {
                "sources": [
                    {"url": "https://example.com", "label": "Site"},
                    {"url": "https://example.org/about", "label": "About"},
                ]
            }
        )
        result = si.collect_sources(entries, None, web_fetcher=fetcher)
        self.assertEqual("collected", result["status"])
        self.assertEqual(2, len(result["sources"]))
        self.assertIn(
            "Visible business information",
            result["sources"][0]["content"]["visible_text"],
        )

    def test_duplicate_sources_are_rejected(self) -> None:
        with self.assertRaisesRegex(si.SourceError, "Duplicate"):
            si.normalize_source_list(
                {
                    "sources": [
                        "https://example.com",
                        "https://example.com",
                    ]
                }
            )

    def test_secret_in_source_review_is_rejected(self) -> None:
        value = {
            "schema_version": "1.0",
            "status": "collected",
            "collected_at": "2026-01-01",
            "sources": [
                {
                    "url": "https://example.com",
                    "final_url": "https://example.com",
                    "label": "Site",
                    "kind": "website",
                    "status": "collected",
                    "retrieved_at": "2026-01-01",
                    "method": "HTTP",
                    "coverage": ["text"],
                    "limitations": [],
                    "content": {"visible_text": "vk1." + ("Z" * 40)},
                }
            ],
            "security": {
                "contains_secrets": False,
                "token_embedded": False,
                "vk_api_used": False,
            },
        }
        errors = si.validate_source_review(value)
        self.assertTrue(any("potential secret" in item for item in errors), errors)


if __name__ == "__main__":
    unittest.main()
