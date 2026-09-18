import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import vk_content_report as report


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_xlsx(path, post_pairs=None):
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Сводка" sheetId="1" r:id="rId1"/>
    <sheet name="Лучшие посты" sheetId="2" r:id="rId2"/>
    <sheet name="Проверенные посты" sheetId="3" r:id="rId3"/>
    <sheet name="Недоступно" sheetId="4" r:id="rId4"/>
  </sheets>
</workbook>"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
</Types>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/xl/worksheets/sheet1.xml" Id="rId1"/>
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/xl/worksheets/sheet2.xml" Id="rId2"/>
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/xl/worksheets/sheet3.xml" Id="rId3"/>
  <Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/xl/worksheets/sheet4.xml" Id="rId4"/>
</Relationships>"""
    post_pairs = post_pairs or [(-1, 1), (-1, 2), (-2, 3), (-2, 4), (-3, 5), (-3, 6)]
    checked_urls = " ".join(
        f"https://vk.com/wall{owner_id}_{post_id}"
        for owner_id, post_id in post_pairs
    )
    checked_sheet = f"""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row><c t="inlineStr"><is><t>{checked_urls}</t></is></c></row></sheetData>
</worksheet>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet2.xml", checked_sheet)
        archive.writestr("xl/worksheets/sheet3.xml", checked_sheet)
        archive.writestr("[Content_Types].xml", content_types)


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.inputs = self.root / "inputs"
        self.run_dir = self.root / "report-run"
        self.dataset_path = self.inputs / "vk_posts_dataset.json"
        self.comments_path = self.inputs / "vk_comments.json"
        self.xlsx_path = self.inputs / "vk_best_posts.xlsx"
        self.validation_path = self.inputs / "final_validation.json"
        self.media_root = self.root
        self._build()

    def _build(self):
        media_dir = self.root / "media"
        (media_dir / "images").mkdir(parents=True)
        (media_dir / "clips").mkdir(parents=True)
        for index in [1, 2, 4, 5, 6]:
            (media_dir / "images" / f"p{index}.jpg").write_bytes(b"image")
        (media_dir / "clips" / "p3.mp4").write_bytes(b"video")
        communities = [
            {
                "community_id": 1,
                "name": "Студия один",
                "vk": "https://vk.com/studio_one",
                "competitor_type": "Косвенный",
                "followers": 1000,
            },
            {
                "community_id": 2,
                "name": "Студия два",
                "vk": "https://vk.com/studio_two",
                "competitor_type": "За внимание",
                "followers": 500,
            },
            {
                "community_id": 3,
                "name": "Студия три",
                "vk": "https://vk.com/studio_three",
                "competitor_type": "Косвенный",
                "followers": 250,
            },
        ]
        raw_posts = [
            (1, 1, 5, 1, 0, 100, "Чтобы узнать стоимость, напишите нам в сообщения."),
            (2, 1, 2, 0, 0, 100, "Расписание занятий на неделю."),
            (3, 2, 10, 2, 1, 100, ""),
            (4, 2, 0, 0, 0, 100, "Общая афиша программы."),
            (5, 3, 2, 0, 0, 20, "Показываем готовую работу участника."),
            (6, 3, 0, 0, 0, 20, "Обычное объявление."),
        ]
        benchmark_by_community = {
            1: (0.06 + 0.02) / 2,
            2: (0.13 + 0.0) / 2,
            3: (0.10 + 0.0) / 2,
        }
        community_names = {item["community_id"]: item for item in communities}
        posts = []
        for index, community_id, likes, comments, reposts, views, text in raw_posts:
            er = (likes + comments + reposts) / views if views else None
            post_id = f"-{community_id}_{index}"
            community = community_names[community_id]
            is_clip = index == 3
            media = {
                "images": (
                    []
                    if is_clip
                    else [
                        {
                            "path": f"media/images/p{index}.jpg",
                            "kind": "photo",
                            "width": 100,
                            "height": 100,
                        }
                    ]
                ),
                "collage_path": None,
                "clip_path": "media/clips/p3.mp4" if is_clip else None,
                "transcription_status": "success" if is_clip else None,
                "transcript": "Сегодня готовим лимонад." if is_clip else None,
            }
            posts.append(
                {
                    "post_id": post_id,
                    "owner_id": -community_id,
                    "vk_post_id": index,
                    "community_id": community_id,
                    "community_name": community["name"],
                    "community_vk": community["vk"],
                    "competitor_type": community["competitor_type"],
                    "followers": community["followers"],
                    "date": "2026-01-01T10:00:00Z",
                    "url": f"https://vk.com/wall{post_id}",
                    "text": text,
                    "post_type": "Клип" if is_clip else "Картинка",
                    "likes": likes,
                    "comments": comments,
                    "reposts": reposts,
                    "views": views,
                    "er": er,
                    "benchmark": benchmark_by_community[community_id],
                    "is_best": er >= benchmark_by_community[community_id],
                    "media": media,
                }
            )
        dataset = {
            "schema_version": "1.0",
            "status": "confirmed",
            "source": {
                "competitor_set_status": "confirmed",
                "competitor_set_sha256": "a" * 64,
            },
            "communities": communities,
            "posts": posts,
            "benchmarks": {
                str(community_id): {
                    "benchmark": value,
                    "valid_post_count": 2,
                    "window_post_count": 2,
                }
                for community_id, value in benchmark_by_community.items()
            },
        }
        comments = {}
        for post in posts:
            post_id = post["post_id"]
            items = []
            if post_id == "-1_1":
                items = [
                    {
                        "comment_id": "c-price",
                        "date": "2026-01-01T11:00:00Z",
                        "text": "Подскажите, пожалуйста, стоимость участия?",
                        "likes": 0,
                        "replies": [],
                    }
                ]
            if post_id == "-2_3":
                items = [
                    {
                        "comment_id": "c-emoji",
                        "date": "2026-01-01T11:00:00Z",
                        "text": "😍😍",
                        "likes": 0,
                        "replies": [],
                    },
                    {
                        "comment_id": "c-recipe",
                        "date": "2026-01-01T11:01:00Z",
                        "text": "Я тоже готовлю лимонад с мятой и лимоном.",
                        "likes": 0,
                        "replies": [],
                    },
                ]
            comments[post_id] = {
                "post_id": post_id,
                "post_url": post["url"],
                "api_post_comment_count": post["comments"],
                "top_level_comments": items,
                "audit": {
                    "top_level_expected": len(items),
                    "top_level_collected": len(items),
                    "replies_expected": 0,
                    "replies_collected": 0,
                },
            }
        write_json(self.dataset_path, dataset)
        write_json(self.comments_path, comments)
        create_xlsx(self.xlsx_path)
        self._rewrite_validation()

    def _rewrite_validation(self):
        validation = {
            "passed": True,
            "status": "confirmed",
            "hashes": {
                "dataset": file_hash(self.dataset_path),
                "comments": file_hash(self.comments_path),
                "xlsx": file_hash(self.xlsx_path),
            },
        }
        write_json(self.validation_path, validation)

    def prepare(self, min_words=450, max_words=1600):
        return report.prepare(
            self.dataset_path,
            self.comments_path,
            self.xlsx_path,
            self.validation_path,
            self.run_dir,
            self.media_root,
            top_n=1,
            min_words=min_words,
            max_words=max_words,
        )

    def findings(self, short=False):
        long_text = (
            "Вывод описывает только наблюдаемую картину этой недели, отделяет относительный ER "
            "от действий и комментариев и не переносит результат на заявки, продажи или устойчивый спрос."
        )
        text = "Короткий вывод." if short else long_text
        specs = {
            "top_quartile": [
                ("scope", "top-scope", "Какие посты вошли", ["er_benchmark"], ["-2_3", "-3_5"], [], ["-2_3", "-3_5"]),
                ("formats", "top-formats", "Форматы", ["descriptive"], ["-2_3"], [], []),
                ("themes", "top-themes", "Темы", ["descriptive"], ["-3_5"], [], []),
                ("cta", "top-cta", "CTA", ["descriptive"], ["-2_3"], [], []),
                ("reactions", "top-reactions", "Реакции", ["descriptive"], ["-2_3"], [], []),
                ("attachments", "top-attachments", "Вложения", ["descriptive"], ["-3_5"], [], []),
            ],
            "bottom_quartile": [
                ("scope", "bottom-scope", "Какие посты вошли", ["descriptive"], ["-2_4", "-3_6"], [], ["-2_4", "-3_6"]),
                ("formats", "bottom-formats", "Форматы", ["descriptive"], ["-2_4"], [], []),
                ("themes", "bottom-themes", "Темы", ["descriptive"], ["-3_6"], [], []),
                ("cta", "bottom-cta", "CTA", ["descriptive"], ["-2_4"], [], []),
                ("reactions", "bottom-reactions", "Реакции", ["descriptive"], ["-3_6"], [], []),
                ("attachments", "bottom-attachments", "Вложения", ["descriptive"], ["-2_4"], [], []),
            ],
            "audience_and_clips": [
                ("audience", "audience-comments", "О чём пишет аудитория", ["content_interest"], ["-2_3"], ["c-recipe"], []),
                ("triggers", "audience-triggers", "Подтверждённые триггеры", ["purchase_motivation"], ["-1_1"], ["c-price"], []),
                ("promises", "author-promises", "Обещания", ["descriptive"], ["-1_1"], [], []),
                ("arguments", "author-arguments", "Аргументы", ["descriptive"], ["-3_5"], [], []),
                ("visuals", "visuals", "Визуалы", ["descriptive"], ["-3_5"], [], []),
                ("tone", "tone", "Тон и сценарий", ["descriptive"], ["-2_3"], [], []),
                ("hooks", "clip-hooks", "Первые секунды клипа", ["descriptive"], ["-2_3"], [], []),
            ],
            "overall": [
                ("current", "overall-current", "Актуальные форматы, темы и CTA", ["descriptive"], ["-2_3", "-3_5"], [], []),
                ("not_current", "overall-not-current", "Неактуальные форматы, темы и CTA", ["descriptive"], ["-2_4", "-3_6"], [], []),
            ],
        }
        sections = []
        for section_id, rows in specs.items():
            findings = []
            for category, finding_id, headline, signals, post_ids, comment_ids, metric_ids in rows:
                quotes = []
                if finding_id == "author-promises":
                    quotes = [
                        {
                            "post_id": "-1_1",
                            "text": "Чтобы узнать стоимость, напишите нам в сообщения.",
                        }
                    ]
                if finding_id == "audience-comments":
                    quotes = [
                        {
                            "comment_id": "c-recipe",
                            "text": "Я тоже готовлю лимонад с мятой и лимоном.",
                        }
                    ]
                if finding_id == "audience-triggers":
                    quotes = [
                        {
                            "comment_id": "c-price",
                            "text": "Подскажите, пожалуйста, стоимость участия?",
                        }
                    ]
                if finding_id == "author-arguments":
                    quotes = [
                        {
                            "post_id": "-3_5",
                            "text": "Показываем готовую работу участника.",
                        }
                    ]
                if finding_id == "clip-hooks":
                    quotes = [
                        {
                            "post_id": "-2_3",
                            "text": "Сегодня готовим лимонад.",
                        }
                    ]
                findings.append(
                    {
                        "finding_id": finding_id,
                        "category": category,
                        "headline": headline,
                        "analysis": text,
                        "signal_types": signals,
                        "evidence_post_ids": post_ids,
                        "metric_post_ids": metric_ids,
                        "comment_evidence_ids": comment_ids,
                        "tier_escalation_reason": (
                            "У конкурентов более высокого уровня нет релевантного материала этой категории."
                        ),
                        "quotes": quotes,
                    }
                )
            sections.append({"id": section_id, "findings": findings})
        return {
            "status": "draft",
            "title": "Что сработало у конкурентов во ВКонтакте за неделю",
            "summary": (
                "Сначала использованы доказательства прямых конкурентов, затем косвенных "
                "и конкурентов за внимание, если данных верхнего уровня не хватало."
            ),
            "visual_review": {
                "xlsx_visual_check_passed": True,
                "xlsx_sheets_checked": [
                    "Сводка",
                    "Лучшие посты",
                    "Проверенные посты",
                    "Недоступно",
                ],
                "media_visual_check_passed": True,
                "media_post_ids_checked": [
                    "-1_1",
                    "-1_2",
                    "-2_3",
                    "-2_4",
                    "-3_5",
                    "-3_6",
                ],
                "transcripts_checked_post_ids": ["-2_3"],
            },
            "sections": sections,
        }


class VkContentReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build_positive(self):
        self.fixture.prepare()
        findings_path = self.fixture.root / "findings.json"
        write_json(findings_path, self.fixture.findings())
        report.build(self.fixture.run_dir, findings_path)
        return report.validate_outputs(
            self.fixture.dataset_path,
            self.fixture.comments_path,
            self.fixture.xlsx_path,
            self.fixture.validation_path,
            self.fixture.run_dir,
            self.fixture.media_root,
        )

    def test_russian_noun_form(self):
        self.assertEqual(report.russian_noun_form(1, "пост", "поста", "постов"), "пост")
        self.assertEqual(report.russian_noun_form(22, "пост", "поста", "постов"), "поста")
        self.assertEqual(report.russian_noun_form(87, "пост", "поста", "постов"), "постов")
        self.assertEqual(report.russian_noun_form(111, "пост", "поста", "постов"), "постов")

    def test_correct_chain(self):
        result = self.build_positive()
        self.assertTrue(result["passed"])
        self.assertEqual(result["counts"]["posts"], 6)
        self.assertEqual(result["counts"]["sections"], 4)
        self.assertEqual(result["counts"]["findings"], 21)
        self.assertEqual(result["counts"]["evidence_blocks"], 21)
        self.assertEqual(result["counts"]["evidence_outside_bullets"], 0)
        self.assertEqual(result["counts"]["rankable_relative_er_posts"], 6)
        self.assertEqual(result["counts"]["quartile_size"], 2)
        self.assertTrue((self.fixture.run_dir / "outputs" / "vk_content_report.html").exists())

    def test_missing_comments(self):
        missing = self.fixture.inputs / "missing-comments.json"
        with self.assertRaisesRegex(report.PipelineError, "Отсутствует обязательный файл"):
            report.prepare(
                self.fixture.dataset_path,
                missing,
                self.fixture.xlsx_path,
                self.fixture.validation_path,
                self.fixture.run_dir,
                self.fixture.media_root,
            )

    def test_mismatched_post_ids(self):
        comments = json.loads(self.fixture.comments_path.read_text(encoding="utf-8"))
        comments.pop("-3_6")
        write_json(self.fixture.comments_path, comments)
        self.fixture._rewrite_validation()
        with self.assertRaisesRegex(report.PipelineError, "Множества post_id"):
            self.fixture.prepare()

    def test_profile_owned_post_id_is_accepted(self):
        dataset = json.loads(self.fixture.dataset_path.read_text(encoding="utf-8"))
        dataset["posts"][0]["post_id"] = "1_1"
        dataset["posts"][0]["owner_id"] = 1
        dataset["posts"][0]["url"] = "https://vk.com/wall1_1"
        write_json(self.fixture.dataset_path, dataset)

        comments = json.loads(self.fixture.comments_path.read_text(encoding="utf-8"))
        block = comments.pop("-1_1")
        block["post_id"] = "1_1"
        block["post_url"] = "https://vk.com/wall1_1"
        comments["1_1"] = block
        write_json(self.fixture.comments_path, comments)

        create_xlsx(
            self.fixture.xlsx_path,
            post_pairs=[(1, 1), (-1, 2), (-2, 3), (-2, 4), (-3, 5), (-3, 6)],
        )
        self.fixture._rewrite_validation()
        result = self.fixture.prepare()
        self.assertTrue(result["analysis_input"].exists())

    def test_comment_audit_must_match_collected_items(self):
        comments = json.loads(self.fixture.comments_path.read_text(encoding="utf-8"))
        comments["-1_1"]["audit"]["top_level_collected"] = 0
        write_json(self.fixture.comments_path, comments)
        self.fixture._rewrite_validation()
        with self.assertRaisesRegex(report.PipelineError, "Аудит комментариев"):
            self.fixture.prepare()

    def test_cta_extraction_covers_common_wording(self):
        phrases = report.extract_cta(
            "Места ещё есть, присоединяйтесь! Смотрите подробности и переходите по ссылке."
        )
        self.assertEqual(len(phrases), 2)

    def test_wrong_er(self):
        dataset = json.loads(self.fixture.dataset_path.read_text(encoding="utf-8"))
        dataset["posts"][0]["er"] = 0.99
        write_json(self.fixture.dataset_path, dataset)
        self.fixture._rewrite_validation()
        with self.assertRaisesRegex(report.PipelineError, "Неверный ER"):
            self.fixture.prepare()

    def test_xlsx_post_urls_must_match_dataset(self):
        create_xlsx(
            self.fixture.xlsx_path,
            post_pairs=[(-1, 1), (-1, 2), (-2, 3), (-2, 4), (-3, 5)],
        )
        self.fixture._rewrite_validation()
        with self.assertRaisesRegex(report.PipelineError, "ссылок на посты в XLSX"):
            self.fixture.prepare()

    def test_unconfirmed_causal_claim(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        cta = findings["sections"][2]["findings"][2]
        cta["signal_types"] = ["descriptive"]
        cta["headline"] = "CTA вызвал действие"
        findings_path = self.fixture.root / "causal.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "Неподтверждённый причинный"):
            report.build(self.fixture.run_dir, findings_path)

    def test_missing_evidence_links(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][0]["findings"][0]["evidence_post_ids"] = []
        findings_path = self.fixture.root / "no-evidence.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "Нет постов-доказательств"):
            report.build(self.fixture.run_dir, findings_path)

    def test_visual_review_is_required(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings.pop("visual_review")
        findings_path = self.fixture.root / "no-visual-review.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "отсутствует visual_review"):
            report.build(self.fixture.run_dir, findings_path)

    def test_all_media_must_be_visually_checked(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["visual_review"]["media_post_ids_checked"].remove("-3_6")
        findings_path = self.fixture.root / "missing-media-review.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "не все посты с локальными медиа"):
            report.build(self.fixture.run_dir, findings_path)

    def test_missing_required_category(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][0]["findings"] = [
            item
            for item in findings["sections"][0]["findings"]
            if item["category"] != "attachments"
        ]
        findings_path = self.fixture.root / "missing-category.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "отсутствуют категории"):
            report.build(self.fixture.run_dir, findings_path)

    def test_quartile_evidence_must_stay_in_quartile(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][0]["findings"][1]["evidence_post_ids"] = ["-1_1"]
        findings_path = self.fixture.root / "wrong-quartile.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "не входит в верхний квартиль"):
            report.build(self.fixture.run_dir, findings_path)

    def test_recommendations_are_rejected(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][0]["findings"][0][
            "analysis"
        ] += " Рекомендуется публиковать этот формат чаще."
        findings_path = self.fixture.root / "recommendations.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "обнаружены рекомендации"):
            report.build(self.fixture.run_dir, findings_path)

    def test_manual_er_numbers_are_rejected(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][0]["findings"][0]["analysis"] += " ER 200%."
        findings_path = self.fixture.root / "manual-er.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "нельзя писать вручную"):
            report.build(self.fixture.run_dir, findings_path)

    def test_ai_style_phrase_is_rejected(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][0]["findings"][0]["analysis"] = (
            "Следует отметить наблюдаемую картину недели."
        )
        findings_path = self.fixture.root / "ai-style.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "ИИ-формулировки"):
            report.build(self.fixture.run_dir, findings_path)

    def test_exact_cta_quote_is_required(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        item = findings["sections"][2]["findings"][2]
        item["headline"] = "CTA"
        item["quotes"] = []
        findings_path = self.fixture.root / "no-cta-quote.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "точной цитаты CTA"):
            report.build(self.fixture.run_dir, findings_path)

    def test_comment_quote_is_required(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][2]["findings"][0]["quotes"] = []
        findings_path = self.fixture.root / "no-comment-quote.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "цитаты комментария"):
            report.build(self.fixture.run_dir, findings_path)

    def test_lower_competitor_tier_requires_reason(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        findings["sections"][0]["findings"][1]["tier_escalation_reason"] = ""
        findings_path = self.fixture.root / "no-escalation-reason.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "более низкого уровня"):
            report.build(self.fixture.run_dir, findings_path)

    def test_non_extreme_er_metric_is_rejected(self):
        self.fixture.prepare()
        findings = self.fixture.findings()
        item = findings["sections"][2]["findings"][2]
        item["metric_post_ids"] = ["-1_1"]
        findings_path = self.fixture.root / "non-extreme-metric.json"
        write_json(findings_path, findings)
        with self.assertRaisesRegex(report.PipelineError, "ER разрешено показывать"):
            report.build(self.fixture.run_dir, findings_path)

    def test_html_wrong_volume(self):
        self.fixture.prepare()
        findings_path = self.fixture.root / "short.json"
        write_json(findings_path, self.fixture.findings(short=True))
        with self.assertRaisesRegex(report.PipelineError, "HTML неправильного объёма"):
            report.build(self.fixture.run_dir, findings_path)


if __name__ == "__main__":
    unittest.main()
