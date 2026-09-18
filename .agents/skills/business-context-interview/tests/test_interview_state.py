from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import interview_state as interview  # noqa: E402


def write_source_review(root: Path) -> Path:
    path = root / "source_review.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "status": "collected",
                "collected_at": "2026-01-01",
                "sources": [
                    {
                        "url": "https://vk.com/example",
                        "status": "collected",
                    }
                ],
                "security": {
                    "contains_secrets": False,
                    "token_embedded": False,
                    "vk_api_used": True,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


class InterviewStateTests(unittest.TestCase):
    def state(self, root: Path) -> dict:
        return interview.create_state(write_source_review(root))

    def test_only_one_question_can_be_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            interview.ask_question(
                state,
                "business",
                "marketing_niche",
                "К какой маркетинговой нише относится бизнес?",
            )
            with self.assertRaisesRegex(interview.InterviewError, "answered first"):
                interview.ask_question(
                    state,
                    "business",
                    "assortment",
                    "Насколько широк ассортимент?",
                )

    def test_incomplete_answer_requires_clarification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            interview.ask_question(
                state,
                "business",
                "pricing",
                "Как устроены цены?",
            )
            interview.answer_question(state, 1, "По-разному.", "general")
            with self.assertRaisesRegex(
                interview.InterviewError,
                "needs clarification",
            ):
                interview.ask_question(
                    state,
                    "business",
                    "audience",
                    "Кто основные покупатели?",
                )
            interview.ask_question(
                state,
                "business",
                "pricing",
                "От чего зависит итоговая цена?",
                clarification_of=1,
            )
            interview.answer_question(
                state,
                2,
                "Цена зависит от выбранного формата.",
                "complete",
            )
            self.assertEqual([], interview.validate_state(state))

    def test_duplicate_question_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            interview.ask_question(
                state,
                "business",
                "sales",
                "Как оформить заказ?",
            )
            interview.answer_question(
                state,
                1,
                "Написать в сообщения сообщества.",
                "complete",
            )
            with self.assertRaisesRegex(interview.InterviewError, "already"):
                interview.ask_question(
                    state,
                    "business",
                    "sales",
                    "Как оформить заказ?",
                )

    def test_all_noncomplete_qualities_block_new_topic(self) -> None:
        for quality in ("general", "incomplete", "contradictory"):
            with self.subTest(quality=quality), tempfile.TemporaryDirectory() as temporary:
                state = self.state(Path(temporary))
                interview.ask_question(
                    state,
                    "business",
                    "assortment",
                    f"Как описать ассортимент для проверки {quality}?",
                )
                interview.answer_question(
                    state,
                    1,
                    "Ответ требует уточнения.",
                    quality,
                )
                with self.assertRaisesRegex(
                    interview.InterviewError,
                    "needs clarification",
                ):
                    interview.ask_question(
                        state,
                        "business",
                        "audience",
                        "Кто покупатель?",
                    )

    def test_compound_question_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            with self.assertRaisesRegex(interview.InterviewError, "one simple"):
                interview.ask_question(
                    state,
                    "business",
                    "sales",
                    "Как оформить заказ? И как его оплатить?",
                )

    def test_business_limit_includes_clarifications(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            for index in range(15):
                interview.ask_question(
                    state,
                    "business",
                    f"topic_{index}",
                    f"Вопрос номер {index + 1}?",
                )
                interview.answer_question(
                    state,
                    index + 1,
                    f"Полный ответ {index + 1}.",
                    "complete",
                )
            with self.assertRaisesRegex(interview.InterviewError, "limit"):
                interview.ask_question(
                    state,
                    "business",
                    "extra",
                    "Ещё один вопрос?",
                )

    def test_competitor_block_is_separate_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            interview.close_business(
                state,
                ["Не подтверждён способ оплаты"],
                additional_round_offered=True,
                additional_round_requested="no",
            )
            for topic in interview.COMPETITOR_TOPICS:
                interview.mark_resolved(state, topic)
            interview.ask_question(
                state,
                "competitors",
                "known_competitors",
                "Есть ли ещё известный конкурент?",
            )
            interview.answer_question(state, 1, "Нет.", "complete")
            interview.close_competitors(state)
            self.assertEqual("complete", state["phase"])
            self.assertEqual(0, state["business_questions_asked"])
            self.assertEqual(1, state["competitor_questions_asked"])
            self.assertEqual([], interview.validate_state(state))

    def test_competitor_block_cannot_close_with_missing_topics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = self.state(Path(temporary))
            interview.close_business(
                state,
                [],
                additional_round_offered=True,
            )
            with self.assertRaisesRegex(
                interview.InterviewError,
                "unresolved required topics",
            ):
                interview.close_competitors(state)


if __name__ == "__main__":
    unittest.main()
