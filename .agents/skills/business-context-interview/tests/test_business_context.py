from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import business_context as bc  # noqa: E402
import interview_state as interview  # noqa: E402


def sample_context() -> dict:
    return {
        "business": {
            "name": "Тестовая студия",
            "description": "Вымышленный бизнес для автоматических тестов.",
            "marketing_niche": "Творческие услуги",
            "broader_categories": ["Досуг"],
            "positioning": "Камерные творческие занятия.",
            "main_differentiator": "Индивидуальный формат."
        },
        "assortment": {
            "breadth": "Средний ассортимент",
            "variation_dimensions": ["Формат занятия"],
            "primary_offers": [
                {
                    "name": "Разовое занятие",
                    "formats": ["Офлайн"],
                    "priority": "primary"
                }
            ],
            "secondary_offers": ["Подарочный сертификат"],
            "product_categories": [
                {
                    "name": "Занятия",
                    "role": "primary",
                    "examples": ["Разовое занятие"]
                }
            ]
        },
        "production": {
            "model": "Собственное оказание услуг",
            "creators_or_suppliers": "Команда студии",
            "process": ["Запись", "Занятие"],
            "capabilities": ["Индивидуальная помощь"],
            "lead_time": "В день посещения",
            "notes": None
        },
        "audience": {
            "primary_payers": ["Взрослые посетители"],
            "segments": [
                {
                    "name": "Взрослые",
                    "tasks": ["Отдых"]
                }
            ]
        },
        "geography": {
            "primary": "Тестовый город",
            "additional": [],
            "sales_format": ["Офлайн"]
        },
        "pricing": {
            "currency": "RUB",
            "structure": "Фиксированная цена",
            "offers": [
                {
                    "name": "Разовое занятие",
                    "amount": 1000,
                    "unit": "за человека",
                    "conditions": None,
                    "confirmed": True
                }
            ]
        },
        "sales": {
            "booking_channels": ["Сообщения VK"],
            "order_process": "Запись в сообщениях",
            "prepayment_required": False,
            "payment_timing": "На месте",
            "payment_methods": ["Карта"],
            "access_or_delivery": "Посещение студии"
        },
        "acquisition": {
            "primary_channels": ["VK"],
            "additional_channels": []
        },
        "online_presence": {
            "vk_url": "https://vk.com/example-studio",
            "website_url": None,
            "other_links": []
        },
        "advantages": ["Камерный формат"],
        "limitations": ["Ограниченная вместимость"],
        "competitors": {
            "known": [],
            "classification_rules": {
                "direct": "Тот же продукт и аудитория.",
                "indirect": "Часть или похожий ассортимент.",
                "attention": "Похожая потребность клиента."
            },
            "classification_factors": [
                "Ассортимент",
                "Аудитория",
                "География",
                "Цена"
            ],
            "search_constraints": {
                "platforms": ["VK", "Онлайн-карты"],
                "target_count": 10,
                "geography": "Тестовый город",
                "audience_size": {
                    "min": 100,
                    "max": 50000
                },
                "freshness_days": 30,
                "required_inclusions": [],
                "exclusions": ["Заброшенные сообщества"]
            },
            "proposed_search_queries": ["творческая студия тестовый город"]
        },
        "remaining_gaps": [],
        "sources": {
            "vk": {
                "url": "https://vk.com/example-studio",
                "retrieved_at": "2026-01-01",
                "method": "VK API",
                "coverage": ["Профиль", "Публикации"],
                "limitations": []
            },
            "website": None
        }
    }


def sample_context_v11() -> dict:
    data = sample_context()
    data["schema_version"] = "1.1"
    data["competitors"]["classification_rules"] = {
        "direct": bc.DIRECT_RULE,
        "indirect": bc.INDIRECT_RULE,
        "attention": bc.ATTENTION_RULE,
    }
    data["competitors"]["classification_factors"] = sorted(
        bc.REQUIRED_CLASSIFICATION_FACTORS
    )
    data["competitors"]["key_features"] = ["Индивидуальный формат"]
    data["competitors"]["matching_policy"] = {
        "must_repeat_in_competitors": False,
        "importance": "preferred",
        "applies_to": ["direct"],
        "rationale": "Особенность важна для сравнения, но не обязательна.",
    }
    data["sources"]["additional"] = []
    return data


class BusinessContextTests(unittest.TestCase):
    def write_input(self, directory: Path, data: dict | None = None) -> Path:
        input_path = directory / "input.json"
        input_path.write_text(
            json.dumps(data or sample_context(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return input_path

    def write_supporting_files(
        self,
        directory: Path,
        limitations: list[str] | None = None,
    ) -> tuple[Path, Path]:
        source_path = directory / "source_review.json"
        source_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "status": "partial" if limitations else "collected",
                    "collected_at": "2026-01-01",
                    "sources": [
                        {
                            "url": "https://vk.com/example-studio",
                            "final_url": "https://vk.com/example-studio",
                            "label": "VK",
                            "kind": "vk",
                            "status": "partial" if limitations else "collected",
                            "retrieved_at": "2026-01-01",
                            "method": "VK API 5.199",
                            "coverage": ["community_profile", "wall_posts"],
                            "limitations": limitations or [],
                            "content": {"community": {"name": "Тестовая студия"}},
                        }
                    ],
                    "security": {
                        "contains_secrets": False,
                        "token_embedded": False,
                        "vk_api_used": True,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        state = interview.create_state(source_path)
        interview.close_business(
            state,
            [],
            additional_round_offered=True,
            additional_round_requested="no",
        )
        for topic in interview.COMPETITOR_TOPICS:
            interview.mark_resolved(state, topic)
        interview.close_competitors(state)
        state_path = directory / "interview_state.json"
        state_path.write_text(
            interview.canonical_json(state),
            encoding="utf-8",
        )
        return source_path, state_path

    def test_confirmed_card_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = self.write_input(root)
            output_dir = root / "run"
            bc.create_artifacts(input_path, output_dir)
            self.assertEqual([], bc.validate_directory(output_dir))

            bc.confirm_artifacts(output_dir, "2026-01-02")
            self.assertEqual([], bc.validate_directory(output_dir))
            confirmed = bc.load_json(output_dir / bc.JSON_NAME)
            self.assertEqual("confirmed", confirmed["status"])
            self.assertEqual("2026-01-02", confirmed["confirmation_date"])

    def test_missing_vk_source_is_rejected(self) -> None:
        data = bc.normalize_draft(sample_context())
        del data["sources"]["vk"]
        errors = bc.validate_context(data)
        self.assertTrue(any("sources.vk" in error for error in errors), errors)

    def test_corrupted_json_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "run"
            bc.create_artifacts(self.write_input(root), output_dir)
            (output_dir / bc.JSON_NAME).write_text("{broken", encoding="utf-8")
            errors = bc.validate_directory(output_dir)
            self.assertTrue(any("invalid JSON" in error for error in errors), errors)

    def test_markdown_desynchronization_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "run"
            bc.create_artifacts(self.write_input(root), output_dir)
            markdown_path = output_dir / bc.MARKDOWN_NAME
            markdown_path.write_text(
                markdown_path.read_text(encoding="utf-8") + "\nmanual edit\n",
                encoding="utf-8"
            )
            errors = bc.validate_directory(output_dir)
            self.assertTrue(any("out of sync" in error for error in errors), errors)

    def test_secret_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output_dir = root / "run"
            bc.create_artifacts(self.write_input(root), output_dir)
            data = bc.load_json(output_dir / bc.JSON_NAME)
            data["business"]["description"] += " vk1." + ("A" * 40)
            (output_dir / bc.JSON_NAME).write_text(
                bc.canonical_json(data),
                encoding="utf-8"
            )
            (output_dir / bc.MARKDOWN_NAME).write_text(
                bc.render_markdown(data),
                encoding="utf-8"
            )
            errors = bc.validate_directory(output_dir)
            self.assertTrue(any("potential secret" in error for error in errors), errors)

    def test_v11_supporting_files_create_synchronized_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path, state_path = self.write_supporting_files(root)
            output_dir = root / "run"
            bc.create_artifacts(
                self.write_input(root, sample_context_v11()),
                output_dir,
                source_path,
                state_path,
            )
            self.assertEqual([], bc.validate_directory(output_dir))
            data = bc.load_json(output_dir / bc.JSON_NAME)
            self.assertEqual("1.1", data["schema_version"])
            self.assertTrue(data["interview"]["additional_round_offered"])
            self.assertEqual(1, len(data["source_reviews"]))
            markdown = (output_dir / bc.MARKDOWN_NAME).read_text(encoding="utf-8")
            self.assertIn("## Проверка исходных ссылок", markdown)
            self.assertIn("## Параметры интервью", markdown)
            bc.record_approval(
                output_dir,
                "Подтверждаю карточку.",
                "2026-01-02",
            )
            bc.confirm_artifacts(output_dir, "2026-01-02")
            self.assertEqual([], bc.validate_directory(output_dir))

    def test_partial_source_needs_acceptance_before_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path, state_path = self.write_supporting_files(
                root,
                ["market.get unavailable"],
            )
            data = sample_context_v11()
            data["source_reviews"] = [
                {
                    "url": "https://vk.com/example-studio",
                    "limitations_accepted": False,
                }
            ]
            input_path = self.write_input(root, data)
            output_dir = root / "run"
            bc.create_artifacts(
                input_path,
                output_dir,
                source_path,
                state_path,
            )
            bc.record_approval(
                output_dir,
                "Подтверждаю карточку с ограничением.",
                "2026-01-02",
            )
            with self.assertRaisesRegex(bc.ContextError, "limitations_accepted"):
                bc.confirm_artifacts(output_dir, "2026-01-02")

            data["source_reviews"][0]["limitations_accepted"] = True
            input_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            bc.sync_artifacts(
                input_path,
                output_dir,
                source_path,
                state_path,
            )
            bc.record_approval(
                output_dir,
                "Подтверждаю исправленную карточку.",
                "2026-01-02",
                replace=True,
            )
            bc.confirm_artifacts(output_dir, "2026-01-02")
            self.assertEqual([], bc.validate_directory(output_dir))

    def test_mandatory_features_must_be_search_inclusions(self) -> None:
        data = sample_context_v11()
        data["competitors"]["matching_policy"] = {
            "must_repeat_in_competitors": True,
            "importance": "required",
            "applies_to": ["direct"],
            "rationale": "Обязательный критерий пользователя.",
        }
        normalized = bc.normalize_draft(data)
        errors = bc.validate_business_rules(normalized)
        self.assertTrue(
            any("mandatory key features" in error for error in errors),
            errors,
        )

    def test_existing_artifacts_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = self.write_input(root)
            output_dir = root / "run"
            bc.create_artifacts(input_path, output_dir)
            with self.assertRaisesRegex(bc.ContextError, "already exist"):
                bc.create_artifacts(input_path, output_dir)

    def test_v11_confirmation_requires_current_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_path, state_path = self.write_supporting_files(root)
            input_path = self.write_input(root, sample_context_v11())
            output_dir = root / "run"
            bc.create_artifacts(
                input_path,
                output_dir,
                source_path,
                state_path,
            )
            with self.assertRaisesRegex(bc.ContextError, "not found"):
                bc.confirm_artifacts(output_dir, "2026-01-02")
            bc.record_approval(
                output_dir,
                "Подтверждаю карточку.",
                "2026-01-02",
            )
            corrected = sample_context_v11()
            corrected["business"]["description"] = "Исправленное описание."
            input_path.write_text(
                json.dumps(corrected, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            bc.sync_artifacts(
                input_path,
                output_dir,
                source_path,
                state_path,
            )
            with self.assertRaisesRegex(bc.ContextError, "changed after approval"):
                bc.confirm_artifacts(output_dir, "2026-01-02")


if __name__ == "__main__":
    unittest.main()
