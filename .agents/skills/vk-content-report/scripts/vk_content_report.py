#!/usr/bin/env python3
"""Validate VK best-post outputs and build an evidence-first HTML report."""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import posixpath
import re
import shutil
import statistics
import sys
from typing import Any
import zipfile
import xml.etree.ElementTree as ET


EXPECTED_SECTIONS = [
    ("top_quartile", "Топ-25% с лучшим ER относительно среднего ER сообщества"),
    ("bottom_quartile", "Топ-25% с худшим ER относительно среднего ER сообщества"),
    ("audience_and_clips", "Что видно по комментариям и клипам"),
    ("overall", "Общий вывод"),
]
REQUIRED_XLSX_SHEETS = {"Сводка", "Лучшие посты", "Проверенные посты", "Недоступно"}
POST_URL_XLSX_SHEETS = {"Лучшие посты", "Проверенные посты"}
REQUIRED_CATEGORIES = {
    "top_quartile": {"scope", "formats", "themes", "cta", "reactions", "attachments"},
    "bottom_quartile": {"scope", "formats", "themes", "cta", "reactions", "attachments"},
    "audience_and_clips": {
        "audience",
        "triggers",
        "promises",
        "arguments",
        "visuals",
        "tone",
        "hooks",
    },
    "overall": {"current", "not_current"},
}
ALLOWED_SIGNALS = {
    "er_benchmark",
    "cta_action",
    "content_interest",
    "purchase_motivation",
    "descriptive",
}
RECOMMENDATION_PATTERNS = [
    re.compile(r"\bрекоменду\w*", re.I),
    re.compile(r"\bсовету\w*", re.I),
    re.compile(r"\bконтент[- ]?план\w*", re.I),
    re.compile(r"\bиде[яи]\s+(?:для\s+)?(?:постов|контента)\b", re.I),
    re.compile(r"\bвам\s+следует\b", re.I),
    re.compile(r"\b(?:нужно|стоит)\s+(?:делать|использовать|добавить|публиковать)\b", re.I),
]
CAUSAL_PATTERNS = [
    re.compile(r"\bвызвал[аио]?\b", re.I),
    re.compile(r"\bприв[её]л[аио]?\s+к\b", re.I),
    re.compile(r"\bстал[ао]?\s+причиной\b", re.I),
    re.compile(r"\bобеспечил[аио]?\b", re.I),
]
MANUAL_ER_METRIC_PATTERNS = [
    re.compile(r"\bER\b.{0,35}\d", re.I),
    re.compile(r"\d(?:[.,]\d+)?\s*(?:%|раз[а]?)?.{0,35}\bER\b", re.I),
    re.compile(r"\bбенчмарк\w*\b.{0,35}\d", re.I),
    re.compile(r"\d(?:[.,]\d+)?\s*(?:%|раз[а]?)?.{0,35}\bбенчмарк\w*\b", re.I),
]
STYLE_PATTERNS = [
    re.compile(r"\bследует отметить\b", re.I),
    re.compile(r"\bважно отметить\b", re.I),
    re.compile(r"\bможно сделать вывод\b", re.I),
    re.compile(r"\bв рамках данного\b", re.I),
    re.compile(r"\bключев(?:ой|ым) инсайт\w*\b", re.I),
    re.compile(r"\bдемонстриру\w+ эффективность\b", re.I),
]
SECRET_PATTERNS = [
    re.compile(r"vk1\.[A-Za-z0-9._-]{20,}"),
    re.compile(r"access_token\s*[:=]\s*[\"']?[A-Za-z0-9._-]{20,}", re.I),
    re.compile(r"authorization\s*:\s*bearer\s+[A-Za-z0-9._-]{20,}", re.I),
]


class PipelineError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PipelineError(f"Отсутствует обязательный файл: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Повреждён JSON {path.name}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_sha256(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise PipelineError(f"Некорректный SHA-256: {label}")
    return text


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def competitor_tier(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "прям" in text:
        return "direct"
    if "косвен" in text:
        return "indirect"
    if "вниман" in text:
        return "attention"
    return "unknown"


def relative_er_value(er: Any, benchmark: Any) -> float | None:
    if er is None or benchmark is None or float(benchmark) <= 0:
        return None
    return float(er) / float(benchmark)


def metric_display_allowed(relative_er: float | None) -> bool:
    return relative_er is not None and (relative_er >= 2.0 or relative_er <= 0.5)


def flatten_comments(items: list[dict[str, Any]], depth: int = 0) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in items or []:
        result.append(
            {
                "comment_id": str(item.get("comment_id", item.get("id", ""))),
                "parent_comment_id": (
                    str(item["parent_comment_id"])
                    if item.get("parent_comment_id") is not None
                    else None
                ),
                "date": item.get("date"),
                "text": str(item.get("text", "")),
                "likes": int(
                    item.get("likes", {}).get("count", 0)
                    if isinstance(item.get("likes"), dict)
                    else item.get("likes", 0) or 0
                ),
                "depth": depth,
            }
        )
        result.extend(
            flatten_comments(
                item.get("replies")
                or (item.get("thread") or {}).get("items")
                or [],
                depth + 1,
            )
        )
    return result


def only_emoji_or_punctuation(text: str) -> bool:
    stripped = re.sub(r"[\W\d_]+", "", text, flags=re.UNICODE)
    return not stripped


def classify_comment(comment: dict[str, Any], post_text: str) -> dict[str, Any]:
    text = comment["text"].strip()
    lower = text.lower()
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text)
    question = "?" in text or bool(
        re.search(
            r"(сколько|стоимост|цена|когда|где|как запис|есть ли|можно ли|подскажите)",
            lower,
        )
    )
    commercial = bool(
        re.search(r"(стоимост|цена|запис|брон|мест|куп|оплат|ссылка|заявк)", lower)
    )
    code_mechanic = bool(
        re.search(
            r"(кодовое слово|напишите слово|пишите слово|слово в комментариях)",
            post_text.lower(),
        )
    )
    code_word = code_mechanic and len(words) <= 3 and not question and not commercial
    generic_short = bool(
        re.fullmatch(
            r"(класс|супер|вау|огонь|хочу|круто|спасибо|да|нет)",
            " ".join(words).lower(),
        )
    )
    low_content = (
        not text
        or only_emoji_or_punctuation(text)
        or (len(words) <= 2 and generic_short and not question and not commercial)
    )
    substantive = not code_word and not low_content and (question or len(words) >= 4)
    if commercial:
        topic = "commercial_inquiry"
    elif re.search(r"(лимонад|рецепт|сироп|мят|лимон|готов)", lower):
        topic = "topic_contribution"
    elif substantive:
        topic = "substantive_other"
    else:
        topic = None
    purchase_motivation = (
        "price_clarity_as_decision_criterion"
        if re.search(r"(стоимост|цена)", lower)
        else None
    )
    if code_word:
        preliminary_class = "code_word"
    elif low_content:
        preliminary_class = "low_content"
    elif question and commercial:
        preliminary_class = "substantive_commercial_question"
    elif substantive:
        preliminary_class = "substantive_topic_response"
    else:
        preliminary_class = "other"
    return {
        **comment,
        "preliminary_class": preliminary_class,
        "flags": {
            "code_word": code_word,
            "low_content": low_content,
            "substantive": substantive,
            "question": question,
            "commercial_intent": commercial,
            "topic_contribution": topic == "topic_contribution",
        },
        "topic": topic,
        "confirmed_purchase_motivation": purchase_motivation,
    }


def extract_cta(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    ctas = [
        part.strip()
        for part in parts
        if re.search(
            r"(пишите|напишите|звоните|позвоните|бронируйте|забронировать|запись|"
            r"подать заявку|оставьте заявку|переходите|регистрируйтесь|заполняйте|"
            r"смотри(?:те)?|нажмите|присоединяйтесь|участвуйте|отправляйте|"
            r"делись|делитесь|сохраняйте|приходите|заглядывай|до встречи|жд[её]м|"
            r"ловите скидку)",
            part,
            re.I,
        )
    ]
    return ctas[:3]


def xlsx_sheet_names(path: Path) -> list[str]:
    if not path.exists():
        raise PipelineError(f"Отсутствует обязательный файл: {path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("xl/workbook.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise PipelineError(f"Повреждён XLSX: {path.name}") from exc
    root = ET.fromstring(xml)
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [sheet.attrib["name"] for sheet in root.findall(".//x:sheet", namespace)]


def xlsx_post_urls(path: Path) -> set[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rel_targets = {
                item.attrib["Id"]: item.attrib["Target"]
                for item in relationships
                if item.attrib.get("Id") and item.attrib.get("Target")
            }
            spreadsheet_ns = {
                "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            }
            relationship_id = (
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            sheet_paths = []
            for sheet in workbook.findall(".//x:sheet", spreadsheet_ns):
                if sheet.attrib.get("name") not in POST_URL_XLSX_SHEETS:
                    continue
                target = rel_targets.get(sheet.attrib.get(relationship_id, ""))
                if not target:
                    continue
                sheet_paths.append(
                    target.lstrip("/")
                    if target.startswith("/")
                    else posixpath.normpath(posixpath.join("xl", target))
                )
            searchable = []
            names = set(archive.namelist())
            for sheet_path in sheet_paths:
                searchable.append(
                    archive.read(sheet_path).decode("utf-8", errors="ignore")
                )
                rel_path = posixpath.join(
                    posixpath.dirname(sheet_path),
                    "_rels",
                    posixpath.basename(sheet_path) + ".rels",
                )
                if rel_path in names:
                    searchable.append(
                        archive.read(rel_path).decode("utf-8", errors="ignore")
                    )
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise PipelineError(f"Повреждён XLSX: {path.name}") from exc
    return set(
        re.findall(r"https://vk\.com/wall-?[1-9]\d*_[1-9]\d*", "\n".join(searchable))
    )


def find_secret_hits(text: str) -> list[str]:
    return [pattern.pattern for pattern in SECRET_PATTERNS if pattern.search(text)]


def validate_source_chain(
    dataset_path: Path,
    comments_path: Path,
    xlsx_path: Path,
    final_validation_path: Path,
    media_root: Path | None = None,
) -> dict[str, Any]:
    dataset = read_json(dataset_path)
    comments = read_json(comments_path)
    final_validation = read_json(final_validation_path)
    if dataset.get("status") != "confirmed":
        raise PipelineError("vk_posts_dataset.json должен иметь status: confirmed")
    if dataset.get("source", {}).get("competitor_set_status") != "confirmed":
        raise PipelineError("Цепочка competitor_set не подтверждена")
    ensure_sha256(
        dataset.get("source", {}).get("competitor_set_sha256"),
        "competitor_set_sha256",
    )
    if final_validation.get("passed") is not True:
        raise PipelineError("Финальная проверка предыдущего этапа не пройдена")
    if final_validation.get("status") != "confirmed":
        raise PipelineError("Финальная проверка предыдущего этапа не подтверждена")

    hashes = {
        "dataset": sha256(dataset_path),
        "comments": sha256(comments_path),
        "xlsx": sha256(xlsx_path),
        "final_validation": sha256(final_validation_path),
    }
    expected_hashes = final_validation.get("hashes", {})
    expected_dataset = expected_hashes.get("dataset")
    expected_comments = expected_hashes.get("comments")
    expected_xlsx = expected_hashes.get("xlsx", expected_hashes.get("workbook"))
    for label, actual, expected in [
        ("dataset", hashes["dataset"], expected_dataset),
        ("comments", hashes["comments"], expected_comments),
        ("xlsx", hashes["xlsx"], expected_xlsx),
    ]:
        if actual != ensure_sha256(expected, f"final_validation.{label}"):
            raise PipelineError(f"Несовпадение хэша: {label}")

    sheet_names = xlsx_sheet_names(xlsx_path)
    missing_sheets = sorted(REQUIRED_XLSX_SHEETS - set(sheet_names))
    if missing_sheets:
        raise PipelineError(f"В XLSX отсутствуют листы: {', '.join(missing_sheets)}")

    posts = dataset.get("posts")
    communities = dataset.get("communities")
    if not isinstance(posts, list) or not posts:
        raise PipelineError("В dataset отсутствуют проверенные посты")
    if not isinstance(communities, list) or not communities:
        raise PipelineError("В dataset отсутствуют сообщества")
    if not isinstance(comments, dict):
        raise PipelineError("JSON комментариев должен быть объектом по post_id")

    post_ids = [str(post.get("post_id", "")) for post in posts]
    # VK wall feeds can legitimately contain a post owned by a user profile
    # (positive owner_id), even when that post is returned in a community wall
    # response. Accept both profile and community wall identifiers.
    if any(not re.fullmatch(r"-?[1-9]\d*_[1-9]\d*", post_id) for post_id in post_ids):
        raise PipelineError("Некорректный post_id")
    if len(post_ids) != len(set(post_ids)):
        raise PipelineError("В dataset обнаружены дубли post_id")
    if set(post_ids) != set(comments):
        raise PipelineError("Множества post_id в dataset и комментариях не совпадают")
    dataset_urls = {str(post.get("url", "")) for post in posts}
    workbook_post_urls = xlsx_post_urls(xlsx_path)
    if workbook_post_urls != dataset_urls:
        missing = sorted(dataset_urls - workbook_post_urls)
        extra = sorted(workbook_post_urls - dataset_urls)
        raise PipelineError(
            "Множество ссылок на посты в XLSX не совпадает с dataset: "
            f"нет в XLSX {missing}; лишние {extra}"
        )

    root = media_root or dataset_path.parent.parent
    media_paths_checked = 0
    for post in posts:
        post_id = str(post["post_id"])
        views = int(post.get("views", 0) or 0)
        er = post.get("er")
        recomputed = (
            (
                int(post.get("likes", 0))
                + int(post.get("comments", 0))
                + int(post.get("reposts", 0))
            )
            / views
            if views > 0
            else None
        )
        if (er is None) != (recomputed is None) or (
            recomputed is not None and abs(float(er) - recomputed) > 1e-12
        ):
            raise PipelineError(f"Неверный ER: {post_id}")
        expected_url = f"https://vk.com/wall{post_id}"
        if post.get("url") != expected_url:
            raise PipelineError(f"Неверная ссылка на пост: {post_id}")
        block = comments[post_id]
        if block.get("post_id") != post_id or block.get("post_url") != expected_url:
            raise PipelineError(f"Комментарии не связаны с постом: {post_id}")
        if int(block.get("api_post_comment_count", post.get("comments", 0))) != int(
            post.get("comments", 0)
        ):
            raise PipelineError(f"Счётчик комментариев не совпадает: {post_id}")
        top_level_comments = block.get("top_level_comments") or []
        if not isinstance(top_level_comments, list):
            raise PipelineError(f"Некорректный список комментариев: {post_id}")
        flattened_comments = flatten_comments(top_level_comments)
        replies_collected = max(0, len(flattened_comments) - len(top_level_comments))
        audit = block.get("audit") or {}
        audit_checks = {
            "top_level_expected": int(post.get("comments", 0)),
            "top_level_collected": len(top_level_comments),
            "replies_collected": replies_collected,
        }
        for audit_key, expected_value in audit_checks.items():
            if audit_key in audit and int(audit[audit_key]) != expected_value:
                raise PipelineError(
                    f"Аудит комментариев не совпадает ({audit_key}): {post_id}"
                )
        media = post.get("media") or {}
        media_paths = [
            *(item.get("path") for item in media.get("images", []) if item.get("path")),
            media.get("collage_path"),
            media.get("clip_path"),
        ]
        for relative in filter(None, media_paths):
            media_paths_checked += 1
            if Path(relative).is_absolute():
                raise PipelineError(f"Абсолютный медиапуть запрещён: {post_id}")
            if not (root / relative).exists():
                raise PipelineError(f"Отсутствует медиа: {post_id} / {relative}")
        if post.get("post_type") == "Клип":
            status = media.get("transcription_status")
            if status not in {"success", "no_speech"}:
                raise PipelineError(f"Необработанный клип: {post_id}")

    for community_id, benchmark in dataset.get("benchmarks", {}).items():
        community_posts = [
            post for post in posts if str(post.get("community_id")) == str(community_id)
        ]
        valid_er = [float(post["er"]) for post in community_posts if post.get("er") is not None]
        recomputed = mean(valid_er)
        stored = benchmark.get("benchmark")
        if (stored is None) != (recomputed is None) or (
            recomputed is not None and abs(float(stored) - recomputed) > 1e-12
        ):
            raise PipelineError(f"Неверный бенчмарк сообщества: {community_id}")
        for post in community_posts:
            post_benchmark = post.get("benchmark")
            if (post_benchmark is None) != (stored is None) or (
                stored is not None and abs(float(post_benchmark) - float(stored)) > 1e-12
            ):
                raise PipelineError(
                    f"Бенчмарк поста не совпадает с бенчмарком сообщества: {post['post_id']}"
                )
            expected_best = (
                post.get("er") is not None
                and float(post["er"]) + 1e-12 >= float(stored)
            )
            if bool(post.get("is_best")) != expected_best:
                raise PipelineError(f"Неверный признак прохождения бенчмарка: {post['post_id']}")

    return {
        "dataset": dataset,
        "comments": comments,
        "final_validation": final_validation,
        "hashes": hashes,
        "sheet_names": sheet_names,
        "xlsx_post_urls": sorted(workbook_post_urls),
        "media_root": root,
        "media_paths_checked": media_paths_checked,
    }


def summarize_posts(posts: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [post for post in posts if post["er"] is not None]
    comments = [comment for post in posts for comment in post["comment_signals"]]
    passed = [post for post in valid if post["benchmark_pass"]]
    code_or_low = [
        item
        for item in comments
        if item["flags"]["code_word"] or item["flags"]["low_content"]
    ]
    return {
        "posts": len(posts),
        "valid_er_posts": len(valid),
        "benchmark_pass_posts": len(passed),
        "benchmark_pass_rate": ratio(len(passed), len(valid)),
        "mean_post_er": mean([post["er"] for post in valid]),
        "comments": len(comments),
        "questions": sum(item["flags"]["question"] for item in comments),
        "substantive_comments": sum(item["flags"]["substantive"] for item in comments),
        "code_word_comments": sum(item["flags"]["code_word"] for item in comments),
        "low_content_comments": sum(item["flags"]["low_content"] for item in comments),
        "code_or_low_share": ratio(len(code_or_low), len(comments)),
    }


def prepare(
    dataset_path: Path,
    comments_path: Path,
    xlsx_path: Path,
    final_validation_path: Path,
    run_dir: Path,
    media_root: Path | None = None,
    top_n: int = 3,
    min_words: int = 450,
    max_words: int = 1600,
) -> dict[str, Path]:
    if top_n < 1:
        raise PipelineError("top_n должен быть положительным")
    if min_words < 100 or max_words <= min_words:
        raise PipelineError("Некорректные границы объёма HTML")
    chain = validate_source_chain(
        dataset_path,
        comments_path,
        xlsx_path,
        final_validation_path,
        media_root,
    )
    dataset = chain["dataset"]
    comments = chain["comments"]
    enriched: list[dict[str, Any]] = []
    for post in dataset["posts"]:
        post_id = str(post["post_id"])
        block = comments[post_id]
        flattened = flatten_comments(block.get("top_level_comments", []))
        classified = [
            classify_comment(comment, str(post.get("text", "")))
            for comment in flattened
        ]
        cta_phrases = extract_cta(str(post.get("text", "")))
        explicit_comment_cta = any(
            re.search(r"(комментар|ответ)", phrase, re.I) for phrase in cta_phrases
        )
        commercial_count = sum(
            comment["flags"]["commercial_intent"] for comment in classified
        )
        if cta_phrases and commercial_count:
            cta_status = "observed_aligned"
        elif explicit_comment_cta and classified:
            cta_status = "observed_aligned"
        elif explicit_comment_cta:
            cta_status = "no_observed_public_action"
        elif cta_phrases:
            cta_status = "off_platform_unobservable"
        else:
            cta_status = "no_explicit_cta"
        media = post.get("media") or {}
        relative_er = relative_er_value(post.get("er"), post.get("benchmark"))
        tier = competitor_tier(post.get("competitor_type"))
        enriched.append(
            {
                "post_id": post_id,
                "community_id": int(post["community_id"]),
                "community_name": post["community_name"],
                "community_vk": post["community_vk"],
                "competitor_type": post["competitor_type"],
                "followers": int(post["followers"]),
                "date": post.get("date"),
                "url": post["url"],
                "text_excerpt": str(post.get("text", ""))[:1600],
                "transcript_excerpt": str(media.get("transcript") or "")[:1200],
                "post_type": post.get("post_type"),
                "likes": int(post.get("likes", 0)),
                "comments": int(post.get("comments", 0)),
                "reposts": int(post.get("reposts", 0)),
                "views": int(post.get("views", 0)),
                "er": post.get("er"),
                "benchmark": post.get("benchmark"),
                "benchmark_pass": bool(post.get("is_best")),
                "relative_er": relative_er,
                "rankable_relative_er": relative_er is not None,
                "metric_display_allowed": metric_display_allowed(relative_er),
                "competitor_tier": tier,
                "evidence_priority": {
                    "direct": 1,
                    "indirect": 2,
                    "attention": 3,
                    "unknown": 4,
                }[tier],
                "cta": {
                    "phrases": cta_phrases,
                    "explicit_comment_cta": explicit_comment_cta,
                    "action_status": cta_status,
                },
                "comment_counts": {
                    "total": len(classified),
                    "questions": sum(item["flags"]["question"] for item in classified),
                    "substantive": sum(
                        item["flags"]["substantive"] for item in classified
                    ),
                    "code_word": sum(item["flags"]["code_word"] for item in classified),
                    "low_content": sum(
                        item["flags"]["low_content"] for item in classified
                    ),
                    "commercial_intent": commercial_count,
                    "code_or_low_share": ratio(
                        sum(
                            item["flags"]["code_word"]
                            or item["flags"]["low_content"]
                            for item in classified
                        ),
                        len(classified),
                    ),
                },
                "comment_signals": classified,
                "comment_classification": (
                    "no_comments" if not classified else "classified_comments"
                ),
                "media": {
                    "image_count": len(media.get("images", [])),
                    "has_collage": bool(media.get("collage_path")),
                    "has_clip": bool(media.get("clip_path")),
                    "transcription_status": media.get("transcription_status"),
                },
            }
        )

    active_communities = {}
    for post in enriched:
        active_communities.setdefault(
            post["community_id"],
            {
                "community_id": post["community_id"],
                "name": post["community_name"],
                "followers": post["followers"],
                "competitor_type": post["competitor_type"],
            },
        )
    top_count = min(top_n, len(active_communities))
    largest_ids = {
        item["community_id"]
        for item in sorted(
            active_communities.values(),
            key=lambda item: (-item["followers"], item["community_id"]),
        )[:top_count]
    }
    community_mean_er = {
        community_id: mean(
            [
                float(post["er"])
                for post in enriched
                if post["community_id"] == community_id and post["er"] is not None
            ]
        )
        for community_id in active_communities
    }
    top_er_ids = {
        community_id
        for community_id, _ in sorted(
            community_mean_er.items(),
            key=lambda item: (
                -(item[1] if item[1] is not None else -1.0),
                item[0],
            ),
        )[:top_count]
    }
    for post in enriched:
        post["size_segment"] = (
            "largest" if post["community_id"] in largest_ids else "smaller"
        )
        post["community_er_segment"] = (
            "top_mean_er" if post["community_id"] in top_er_ids else "lower_mean_er"
        )

    rankable_posts = sorted(
        [post for post in enriched if post["rankable_relative_er"]],
        key=lambda post: (-float(post["relative_er"]), post["post_id"]),
    )
    quartile_size = math.ceil(len(rankable_posts) * 0.25) if rankable_posts else 0
    top_quartile_ids = [post["post_id"] for post in rankable_posts[:quartile_size]]
    bottom_quartile_ids = [
        post["post_id"]
        for post in sorted(
            rankable_posts[-quartile_size:] if quartile_size else [],
            key=lambda post: (float(post["relative_er"]), post["post_id"]),
        )
    ]
    unrankable_ids = [
        post["post_id"] for post in enriched if not post["rankable_relative_er"]
    ]
    quartiles = {
        "rankable_posts": len(rankable_posts),
        "quartile_size": quartile_size,
        "top_post_ids": top_quartile_ids,
        "bottom_post_ids": bottom_quartile_ids,
        "unrankable_post_ids": unrankable_ids,
    }
    tier_counts = {
        tier: sum(post["competitor_tier"] == tier for post in enriched)
        for tier in ["direct", "indirect", "attention", "unknown"]
    }
    used_tiers = [
        tier for tier in ["direct", "indirect", "attention", "unknown"] if tier_counts[tier]
    ]
    priority_scope = {
        "rule": ["direct", "indirect", "attention"],
        "counts": tier_counts,
        "used_tiers": used_tiers,
        "note": (
            "Анализировать все посты; внутри каждого вывода выбирать доказательства "
            "сначала прямых, затем косвенных конкурентов, затем конкурентов за внимание."
        ),
    }

    format_names = sorted({post["post_type"] for post in enriched})
    competitor_types = sorted({post["competitor_type"] for post in enriched})
    aggregates = {
        "overall": summarize_posts(enriched),
        "formats": {
            name: summarize_posts([post for post in enriched if post["post_type"] == name])
            for name in format_names
        },
        "competitor_types": {
            name: summarize_posts(
                [post for post in enriched if post["competitor_type"] == name]
            )
            for name in competitor_types
        },
        "segments": {
            "largest": summarize_posts(
                [post for post in enriched if post["size_segment"] == "largest"]
            ),
            "smaller": summarize_posts(
                [post for post in enriched if post["size_segment"] == "smaller"]
            ),
            "top_mean_er": summarize_posts(
                [
                    post
                    for post in enriched
                    if post["community_er_segment"] == "top_mean_er"
                ]
            ),
            "lower_mean_er": summarize_posts(
                [
                    post
                    for post in enriched
                    if post["community_er_segment"] == "lower_mean_er"
                ]
            ),
        },
        "segment_definitions": {
            "top_n": top_count,
            "largest_community_ids": sorted(largest_ids),
            "top_mean_er_community_ids": sorted(top_er_ids),
        },
    }
    outputs_dir = run_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    source = {
        "dataset_file": dataset_path.name,
        "dataset_sha256": chain["hashes"]["dataset"],
        "comments_file": comments_path.name,
        "comments_sha256": chain["hashes"]["comments"],
        "xlsx_file": xlsx_path.name,
        "xlsx_sha256": chain["hashes"]["xlsx"],
        "final_validation_file": final_validation_path.name,
        "final_validation_sha256": chain["hashes"]["final_validation"],
    }
    signals = {
        "schema_version": "1.0",
        "status": "draft",
        "source": source,
        "classification_note": (
            "Предварительная смысловая разметка: кодовые и низкосодержательные "
            "комментарии не подтверждают интерес к содержанию."
        ),
        "summary": summarize_posts(enriched),
        "posts": [
            {
                "post_id": post["post_id"],
                "post_url": post["url"],
                "competitor_type": post["competitor_type"],
                "competitor_tier": post["competitor_tier"],
                "relative_er": post["relative_er"],
                "cta": post["cta"],
                "comment_counts": post["comment_counts"],
                "classification": post["comment_classification"],
                "comments": post["comment_signals"],
            }
            for post in enriched
        ],
    }
    compact = {
        "schema_version": "1.0",
        "status": "draft",
        "source": source,
        "limits": {
            "min_visible_words": min_words,
            "max_visible_words": max_words,
            "max_quote_chars": 200,
        },
        "required_sections": [
            {"id": section_id, "title": title}
            for section_id, title in EXPECTED_SECTIONS
        ],
        "aggregates": aggregates,
        "quartiles": quartiles,
        "competitor_priority": priority_scope,
        "posts": enriched,
    }
    signals_path = outputs_dir / "comment-signals.json"
    compact_path = outputs_dir / "analysis_input.json"
    write_json(signals_path, signals)
    write_json(compact_path, compact)
    serialized = json.dumps([signals, compact], ensure_ascii=False)
    hits = find_secret_hits(serialized)
    if hits:
        raise PipelineError("В подготовленных выходах обнаружен секрет")
    return {
        "comment_signals": signals_path,
        "analysis_input": compact_path,
    }


def post_map(analysis_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {post["post_id"]: post for post in analysis_input["posts"]}


def comment_map(analysis_input: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for post in analysis_input["posts"]:
        for comment in post["comment_signals"]:
            if comment["comment_id"]:
                result[comment["comment_id"]] = {
                    **comment,
                    "post_id": post["post_id"],
                }
    return result


def validate_findings(
    findings: dict[str, Any],
    analysis_input: dict[str, Any],
) -> None:
    if findings.get("status") not in {"draft", "confirmed"}:
        raise PipelineError("findings.json должен иметь status draft/confirmed")
    sections = findings.get("sections")
    if not isinstance(sections, list):
        raise PipelineError("findings.json не содержит sections")
    expected_ids = [section_id for section_id, _ in EXPECTED_SECTIONS]
    actual_ids = [section.get("id") for section in sections]
    if actual_ids != expected_ids:
        raise PipelineError("Неверный порядок или состав разделов findings.json")
    posts = post_map(analysis_input)
    comments = comment_map(analysis_input)
    quartiles = analysis_input.get("quartiles") or {}
    top_ids = set(quartiles.get("top_post_ids") or [])
    bottom_ids = set(quartiles.get("bottom_post_ids") or [])
    visual_review = findings.get("visual_review")
    if not isinstance(visual_review, dict):
        raise PipelineError("В findings.json отсутствует visual_review")
    if visual_review.get("xlsx_visual_check_passed") is not True:
        raise PipelineError("Визуальная сверка XLSX не подтверждена")
    if set(visual_review.get("xlsx_sheets_checked") or []) != REQUIRED_XLSX_SHEETS:
        raise PipelineError("Визуально проверены не все обязательные листы XLSX")
    if visual_review.get("media_visual_check_passed") is not True:
        raise PipelineError("Визуальная сверка изображений и клипов не подтверждена")
    required_media_ids = {
        post_id
        for post_id, post in posts.items()
        if post["media"]["image_count"]
        or post["media"]["has_collage"]
        or post["media"]["has_clip"]
    }
    reviewed_media_ids = {
        str(value) for value in visual_review.get("media_post_ids_checked") or []
    }
    if reviewed_media_ids != required_media_ids:
        raise PipelineError("Визуально проверены не все посты с локальными медиа")
    required_clip_ids = {
        post_id for post_id, post in posts.items() if post["post_type"] == "Клип"
    }
    reviewed_transcript_ids = {
        str(value)
        for value in visual_review.get("transcripts_checked_post_ids") or []
    }
    if reviewed_transcript_ids != required_clip_ids:
        raise PipelineError("Проверены не все расшифровки клипов")
    all_text = [str(findings.get("title", "")), str(findings.get("summary", ""))]
    finding_ids = set()
    categories_by_section: dict[str, set[str]] = {}
    for section in sections:
        section_id = str(section.get("id", ""))
        categories_by_section[section_id] = set()
        if section_id == "top_quartile":
            section_pool_ids = top_ids
        elif section_id == "bottom_quartile":
            section_pool_ids = bottom_ids
        else:
            section_pool_ids = set(posts)
        best_section_priority = min(
            (posts[post_id]["evidence_priority"] for post_id in section_pool_ids),
            default=4,
        )
        section_findings = section.get("findings")
        if not isinstance(section_findings, list) or not section_findings:
            raise PipelineError(f"Раздел {section_id} не содержит выводов")
        for finding in section_findings:
            finding_id = str(finding.get("finding_id", ""))
            if not re.fullmatch(r"[a-z0-9-]{3,64}", finding_id):
                raise PipelineError("Некорректный finding_id")
            if finding_id in finding_ids:
                raise PipelineError(f"Дубль finding_id: {finding_id}")
            finding_ids.add(finding_id)
            headline = str(finding.get("headline", "")).strip()
            analysis = str(finding.get("analysis", "")).strip()
            if not headline or not analysis:
                raise PipelineError(f"Пустой вывод: {finding_id}")
            if len(headline) > 90 or len(analysis) > 420:
                raise PipelineError(f"Слишком длинный аналитический буллит: {finding_id}")
            if len(re.findall(r"[.!?](?:\s|$)", analysis)) > 2 or analysis.count(",") > 2:
                raise PipelineError(
                    f"Перечисление нужно разделить на отдельные буллиты: {finding_id}"
                )
            category = str(finding.get("category", "")).strip()
            if category not in REQUIRED_CATEGORIES.get(section_id, set()):
                raise PipelineError(
                    f"Некорректная категория {category or '<empty>'} в разделе {section_id}: {finding_id}"
                )
            categories_by_section[section_id].add(category)
            all_text.extend([headline, analysis])
            signal_types = set(finding.get("signal_types") or [])
            if not signal_types or not signal_types.issubset(ALLOWED_SIGNALS):
                raise PipelineError(f"Некорректные signal_types: {finding_id}")
            evidence_ids = [str(value) for value in finding.get("evidence_post_ids") or []]
            if not evidence_ids:
                raise PipelineError(f"Нет постов-доказательств: {finding_id}")
            if any(post_id not in posts for post_id in evidence_ids):
                raise PipelineError(f"Неизвестный post_id в доказательствах: {finding_id}")
            if section_id == "top_quartile" and any(
                post_id not in top_ids for post_id in evidence_ids
            ):
                raise PipelineError(
                    f"Доказательство не входит в верхний квартиль: {finding_id}"
                )
            if section_id == "bottom_quartile" and any(
                post_id not in bottom_ids for post_id in evidence_ids
            ):
                raise PipelineError(
                    f"Доказательство не входит в нижний квартиль: {finding_id}"
                )
            evidence_posts = [posts[post_id] for post_id in evidence_ids]
            uses_lower_tier = any(
                post["evidence_priority"] > best_section_priority
                for post in evidence_posts
            )
            escalation_reason = str(
                finding.get("tier_escalation_reason", "")
            ).strip()
            if uses_lower_tier and len(escalation_reason) < 20:
                raise PipelineError(
                    f"Не объяснено подключение более низкого уровня конкурентов: {finding_id}"
                )
            metric_ids = [str(value) for value in finding.get("metric_post_ids") or []]
            if any(post_id not in evidence_ids for post_id in metric_ids):
                raise PipelineError(
                    f"Числовая метрика не связана с доказательством: {finding_id}"
                )
            if any(not posts[post_id]["metric_display_allowed"] for post_id in metric_ids):
                raise PipelineError(
                    f"ER разрешено показывать только при отклонении в 2 раза: {finding_id}"
                )
            if "er_benchmark" in signal_types and not any(
                post["benchmark_pass"] for post in evidence_posts
            ):
                raise PipelineError(f"ER-вывод не подтверждён: {finding_id}")
            if "cta_action" in signal_types and not any(
                post["cta"]["action_status"] == "observed_aligned"
                for post in evidence_posts
            ):
                raise PipelineError(f"CTA-причинность не подтверждена: {finding_id}")
            if "content_interest" in signal_types and not any(
                post["comment_counts"]["substantive"] > 0 for post in evidence_posts
            ):
                raise PipelineError(
                    f"Содержательный интерес не подтверждён: {finding_id}"
                )
            comment_ids = [
                str(value) for value in finding.get("comment_evidence_ids") or []
            ]
            if any(comment_id not in comments for comment_id in comment_ids):
                raise PipelineError(
                    f"Неизвестный comment_id в доказательствах: {finding_id}"
                )
            if "purchase_motivation" in signal_types and not any(
                comments.get(comment_id, {}).get("confirmed_purchase_motivation")
                for comment_id in comment_ids
            ):
                raise PipelineError(
                    f"Покупательская мотивация не подтверждена: {finding_id}"
                )
            causal = any(
                pattern.search(f"{headline} {analysis}") for pattern in CAUSAL_PATTERNS
            )
            if causal and (
                "cta_action" not in signal_types
                or not any(
                    post["cta"]["action_status"] == "observed_aligned"
                    for post in evidence_posts
                )
            ):
                raise PipelineError(
                    f"Неподтверждённый причинный вывод: {finding_id}"
                )
            quotes = finding.get("quotes") or []
            for quote in quotes:
                quote_text = str(quote.get("text", "")).strip()
                if not quote_text or len(quote_text) > analysis_input["limits"]["max_quote_chars"]:
                    raise PipelineError(f"Некорректная цитата: {finding_id}")
                post_id = str(quote.get("post_id", ""))
                comment_id = str(quote.get("comment_id", ""))
                if post_id:
                    if post_id not in posts:
                        raise PipelineError(f"Цитата с неизвестным post_id: {finding_id}")
                    if post_id not in evidence_ids:
                        raise PipelineError(
                            f"Цитата поста не связана с доказательством: {finding_id}"
                        )
                    haystack = (
                        posts[post_id]["text_excerpt"]
                        + " "
                        + posts[post_id]["transcript_excerpt"]
                    )
                elif comment_id:
                    if comment_id not in comments:
                        raise PipelineError(
                            f"Цитата с неизвестным comment_id: {finding_id}"
                        )
                    if comments[comment_id]["post_id"] not in evidence_ids:
                        raise PipelineError(
                            f"Цитата комментария не связана с доказательством: {finding_id}"
                        )
                    if comment_id not in comment_ids:
                        raise PipelineError(
                            f"Цитата комментария отсутствует в comment_evidence_ids: {finding_id}"
                        )
                    haystack = comments[comment_id]["text"]
                else:
                    raise PipelineError(f"Цитата без источника: {finding_id}")
                if quote_text not in haystack:
                    raise PipelineError(f"Цитата не найдена в источнике: {finding_id}")
            quote_post_ids = {
                str(quote.get("post_id")) for quote in quotes if quote.get("post_id")
            }
            quote_comment_ids = {
                str(quote.get("comment_id"))
                for quote in quotes
                if quote.get("comment_id")
            }
            cta_phrases = {
                post["post_id"]: post["cta"]["phrases"] for post in evidence_posts
            }
            cta_quote_required = category == "cta" or "cta" in (
                f"{headline} {analysis}"
            ).lower()
            if cta_quote_required and any(cta_phrases.values()):
                exact_cta_quote = any(
                    quote.get("post_id") in cta_phrases
                    and any(
                        str(quote.get("text", "")) in phrase
                        for phrase in cta_phrases[str(quote["post_id"])]
                    )
                    for quote in quotes
                    if quote.get("post_id")
                )
                if not exact_cta_quote:
                    raise PipelineError(f"Нет точной цитаты CTA: {finding_id}")
            if category in {"promises", "arguments", "hooks"}:
                has_source_text = any(
                    post["text_excerpt"].strip() or post["transcript_excerpt"].strip()
                    for post in evidence_posts
                )
                if has_source_text and not quote_post_ids:
                    raise PipelineError(
                        f"Нет точной цитаты для категории {category}: {finding_id}"
                    )
            if category in {"audience", "triggers"}:
                has_comment_signal = bool(comment_ids) or any(
                    post["comment_counts"]["substantive"] > 0
                    for post in evidence_posts
                )
                if has_comment_signal and not quote_comment_ids:
                    raise PipelineError(
                        f"Нет точной цитаты комментария: {finding_id}"
                    )
    for section_id, required in REQUIRED_CATEGORIES.items():
        missing = sorted(required - categories_by_section.get(section_id, set()))
        if missing:
            raise PipelineError(
                f"В разделе {section_id} отсутствуют категории: {', '.join(missing)}"
            )
    full_text = " ".join(all_text)
    if any(pattern.search(full_text) for pattern in RECOMMENDATION_PATTERNS):
        raise PipelineError("В findings.json обнаружены рекомендации")
    if any(pattern.search(full_text) for pattern in MANUAL_ER_METRIC_PATTERNS):
        raise PipelineError(
            "Числовые ER и бенчмарки нельзя писать вручную; используйте metric_post_ids"
        )
    if any(pattern.search(full_text) for pattern in STYLE_PATTERNS):
        raise PipelineError("В findings.json обнаружены шаблонные ИИ-формулировки")


def format_percent(value: float | None) -> str:
    return "ER не рассчитан" if value is None else f"{value:.2%}".replace(".", ",")


def format_ratio(value: float | None) -> str:
    return "не рассчитано" if value is None else f"{value:.2f}".replace(".", ",")


def russian_noun_form(value: int, one: str, few: str, many: str) -> str:
    value = abs(int(value))
    if value % 100 in range(11, 15):
        return many
    if value % 10 == 1:
        return one
    if value % 10 in range(2, 5):
        return few
    return many


def render_finding(
    finding: dict[str, Any],
    posts: dict[str, dict[str, Any]],
    comments: dict[str, dict[str, Any]],
) -> str:
    evidence_posts = [posts[str(post_id)] for post_id in finding["evidence_post_ids"]]
    metric_ids = {str(value) for value in finding.get("metric_post_ids") or []}
    metric_rows = []
    for post in evidence_posts:
        if post["post_id"] not in metric_ids:
            continue
        metric_rows.append(
            '<div class="metric-row">'
            f"<strong>{html.escape(post['post_id'])}:</strong> "
            f"реакции — {format_percent(post['er'])}. Обычно здесь — "
            f"{format_percent(post['benchmark'])}. Разница — {format_ratio(post['relative_er'])}×."
            "</div>"
        )
    metric_html = "".join(metric_rows)
    quote_rows = []
    if finding.get("quotes"):
        for quote in finding["quotes"]:
            source = (
                f"пост {quote['post_id']}"
                if quote.get("post_id")
                else f"комментарий {quote['comment_id']}"
            )
            quote_rows.append(
                '<div class="quote-row">'
                f'«{html.escape(quote["text"])}» '
                f'<span class="quote-source">— {html.escape(source)}</span>'
                "</div>"
            )
    links = " · ".join(
        f'<a href="{html.escape(post["url"])}" target="_blank" rel="noopener">'
        f'{html.escape(post["post_id"])}</a>'
        for post in evidence_posts
    )
    return (
        f'<li class="analysis-bullet" data-finding-id="{html.escape(finding["finding_id"])}" '
        f'data-category="{html.escape(finding["category"])}">'
        f'<span class="bullet-text"><strong>{html.escape(finding["headline"])}</strong> '
        f'{html.escape(finding["analysis"])}</span>'
        f'{metric_html}{"".join(quote_rows)}'
        f'<div class="post-example"><strong>Пост-пример:</strong> {links}</div>'
        "</li>"
    )


def build_html_document(
    findings: dict[str, Any],
    analysis_input: dict[str, Any],
    signals_hash: str,
    analysis_hash: str,
    findings_hash: str,
) -> str:
    posts = post_map(analysis_input)
    comments = comment_map(analysis_input)
    section_titles = dict(EXPECTED_SECTIONS)
    rendered_sections = []
    for section in findings["sections"]:
        rendered = "".join(
            render_finding(finding, posts, comments)
            for finding in section["findings"]
        )
        rendered_sections.append(
            f'<section id="{html.escape(section["id"])}">'
            f"<h2>{html.escape(section_titles[section['id']])}</h2>"
            f'<ul class="analysis-list">{rendered}</ul></section>'
        )
    aggregate = analysis_input["aggregates"]["overall"]
    quartiles = analysis_input["quartiles"]
    post_word = russian_noun_form(aggregate["posts"], "пост", "поста", "постов")
    rankable_word = russian_noun_form(
        quartiles["rankable_posts"], "пост", "поста", "постов"
    )
    quartile_word = russian_noun_form(
        quartiles["quartile_size"], "пост", "поста", "постов"
    )
    unrankable_count = len(quartiles["unrankable_post_ids"])
    unrankable_word = russian_noun_form(
        unrankable_count, "пост", "поста", "постов"
    )
    priority = analysis_input["competitor_priority"]
    tier_counts = priority["counts"]
    confirmation = (
        f"Подтверждено: {html.escape(str(findings.get('confirmed_at')))}."
        if findings.get("status") == "confirmed"
        else "Статус: черновик. Ждём подтверждения пользователя."
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="vk-content-report-status" content="{html.escape(findings['status'])}">
  <meta name="analysis-input-sha256" content="{analysis_hash}">
  <meta name="comment-signals-sha256" content="{signals_hash}">
  <meta name="findings-sha256" content="{findings_hash}">
  <title>{html.escape(findings['title'])}</title>
  <style>
    :root{{--ink:#17232b;--muted:#5b6870;--teal:#006b68;--line:#d8e2e1;
      --soft:#f2f7f6;--bg:#edf2f1;--link:#005eb8}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);
      font:13px/1.34 Arial,sans-serif}} main{{width:min(980px,calc(100% - 28px));
      margin:18px auto;background:#fff;padding:28px 34px;box-shadow:0 2px 16px #17352f18}}
    h1{{margin:0 0 7px;color:#073f3d;font-size:25px;line-height:1.12}}
    h2{{margin:20px 0 8px;padding-bottom:5px;border-bottom:2px solid var(--teal);
      color:#073f3d;font-size:18px}} p{{margin:5px 0}} .lead{{color:#2e3d43;font-size:14px}}
    .scope{{margin:11px 0 12px;padding:9px 11px;background:var(--soft);
      border-left:4px solid var(--teal)}} .analysis-list{{margin:3px 0 0;padding-left:20px}}
    .analysis-bullet{{margin:0 0 7px;padding:0 0 7px 1px;border-bottom:1px solid var(--line);
      break-inside:avoid}} .bullet-text strong{{color:var(--teal)}}
    .metric-row,.quote-row{{margin:2px 0 0 10px;color:#3f4f57;font-size:11px}}
    .quote-source{{color:var(--muted)}} .post-example{{margin:2px 0 0 10px;
      color:var(--muted);font-size:11px;line-height:1.25}} a{{color:var(--link);
      text-decoration:underline;text-underline-offset:2px}} footer{{margin-top:14px;padding-top:8px;
      border-top:1px solid var(--line);color:var(--muted);font-size:11px}}
    @media(max-width:640px){{main{{width:100%;margin:0;padding:22px 18px;box-shadow:none}}
      h1{{font-size:22px}}}} @media print{{@page{{size:A4;margin:8mm}} body{{background:#fff;
      font-size:8.15px;line-height:1.2}} main{{width:auto;margin:0;padding:0;box-shadow:none}}
      h1{{font-size:16px}}h2{{font-size:11px;margin-top:8px}}.lead{{font-size:9px}}
      .scope{{margin:5px 0;padding:5px 7px}}.analysis-bullet{{margin-bottom:2.5px;
      padding-bottom:2.5px}}.metric-row,.quote-row,.post-example,footer{{font-size:7px}}}}
  </style>
</head>
<body><main>
  <header>
    <h1>{html.escape(findings['title'])}</h1>
    <p class="lead">На этой неделе ваши конкуренты выпустили <strong>{aggregate['posts']} {post_word}</strong>.
      Сравнить с обычным уровнем их сообществ получилось у {quartiles['rankable_posts']} {rankable_word}.
      В группу с самыми сильными реакциями попали {quartiles['quartile_size']} {quartile_word}.
      Столько же — в группу с самыми слабыми реакциями. Вне сравнения — {unrankable_count} {unrankable_word}.</p>
    <div class="scope"><strong>Кого мы изучали.</strong> Прямых конкурентов —
      {tier_counts['direct']}; косвенных — {tier_counts['indirect']}; конкурентов за внимание —
      {tier_counts['attention']}. {html.escape(findings['summary'])}</div>
  </header>
  {''.join(rendered_sections)}
  <footer>{confirmation} Все цифры, ссылки, картинки и расшифровки проверены.
    Отчёт показывает только то, что видно в собранных данных.</footer>
</main></body></html>
"""


class ReportHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sections: list[str] = []
        self.findings = 0
        self.evidence_blocks = 0
        self.links: list[str] = []
        self.invalid_link_attributes: list[str] = []
        self.evidence_outside_bullets = 0
        self.bullets: list[dict[str, Any]] = []
        self.visible: list[str] = []
        self.meta: dict[str, str] = {}
        self._hidden_depth = 0
        self._current_bullet: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag in {"style", "script"}:
            self._hidden_depth += 1
        if tag == "section":
            self.sections.append(values.get("id", ""))
        if tag == "li" and "analysis-bullet" in classes:
            self.findings += 1
            self._current_bullet = {
                "finding_id": values.get("data-finding-id", ""),
                "evidence_blocks": 0,
                "links": [],
            }
        if tag == "div" and "post-example" in classes:
            self.evidence_blocks += 1
            if self._current_bullet is None:
                self.evidence_outside_bullets += 1
            else:
                self._current_bullet["evidence_blocks"] += 1
        if tag == "a":
            href = values.get("href", "")
            self.links.append(href)
            if values.get("target") != "_blank" or "noopener" not in (
                values.get("rel") or ""
            ).split():
                self.invalid_link_attributes.append(href)
            if self._current_bullet is not None:
                self._current_bullet["links"].append(href)
        if tag == "meta" and values.get("name"):
            self.meta[values["name"]] = values.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)
        if tag == "li" and self._current_bullet is not None:
            self.bullets.append(self._current_bullet)
            self._current_bullet = None

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self.visible.append(data)


def validate_html(
    html_path: Path,
    analysis_input: dict[str, Any],
    expected_hashes: dict[str, str] | None = None,
    findings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = html_path.read_text(encoding="utf-8")
    parser = ReportHTMLParser()
    parser.feed(text)
    issues = []
    expected_ids = [section_id for section_id, _ in EXPECTED_SECTIONS]
    if parser.sections != expected_ids:
        issues.append("Неверный состав или порядок HTML-разделов")
    if parser.findings != parser.evidence_blocks:
        issues.append("Число аналитических буллитов и строк Пост-пример не совпадает")
    if parser.evidence_outside_bullets:
        issues.append("Строка Пост-пример размещена вне аналитического буллита")
    for bullet in parser.bullets:
        if bullet["evidence_blocks"] != 1:
            issues.append(
                f"В буллите {bullet['finding_id']} должно быть ровно одно поле Пост-пример"
            )
        if not bullet["links"]:
            issues.append(f"В буллите {bullet['finding_id']} нет ссылки на пост-пример")
    if not parser.links:
        issues.append("В HTML отсутствуют доказательные ссылки")
    if parser.invalid_link_attributes:
        issues.append("Ссылки на посты должны открываться в новой вкладке с rel=noopener")
    post_urls = {post["post_id"]: post["url"] for post in analysis_input["posts"]}
    valid_post_ids = set(post_urls)
    for link in parser.links:
        match = re.fullmatch(r"https://vk\.com/wall(-\d+_\d+)", link)
        if not match or match.group(1) not in valid_post_ids:
            issues.append(f"Неверная доказательная ссылка: {link}")
    if findings:
        expected_links = {
            finding["finding_id"]: [
                post_urls[post_id]
                for post_id in [str(value) for value in finding["evidence_post_ids"]]
            ]
            for section in findings["sections"]
            for finding in section["findings"]
        }
        actual_links = {
            bullet["finding_id"]: bullet["links"] for bullet in parser.bullets
        }
        if actual_links != expected_links:
            issues.append("Ссылки Пост-пример не совпадают с evidence_post_ids буллитов")
    visible_text = re.sub(r"\s+", " ", " ".join(parser.visible)).strip()
    word_count = len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", visible_text))
    limits = analysis_input["limits"]
    if not (limits["min_visible_words"] <= word_count <= limits["max_visible_words"]):
        issues.append(
            f"HTML неправильного объёма: {word_count} слов; допустимо "
            f"{limits['min_visible_words']}–{limits['max_visible_words']}"
        )
    if any(pattern.search(visible_text) for pattern in RECOMMENDATION_PATTERNS):
        issues.append("В HTML обнаружены рекомендации")
    hits = find_secret_hits(text)
    if hits:
        issues.append("В HTML обнаружен секрет")
    if expected_hashes:
        for key, expected in expected_hashes.items():
            if parser.meta.get(key) != expected:
                issues.append(f"Повреждена цепочка происхождения HTML: {key}")
    return {
        "passed": not issues,
        "issues": issues,
        "counts": {
            "sections": len(parser.sections),
            "findings": parser.findings,
            "evidence_blocks": parser.evidence_blocks,
            "evidence_outside_bullets": parser.evidence_outside_bullets,
            "links": len(parser.links),
            "visible_words": word_count,
        },
        "secret_scan": {"passed": not hits, "hits": hits},
    }


def build(run_dir: Path, findings_path: Path) -> Path:
    outputs = run_dir / "outputs"
    analysis_path = outputs / "analysis_input.json"
    signals_path = outputs / "comment-signals.json"
    analysis_input = read_json(analysis_path)
    findings = read_json(findings_path)
    validate_findings(findings, analysis_input)
    findings_output = outputs / "report_findings.json"
    write_json(findings_output, findings)
    hashes = {
        "analysis-input-sha256": sha256(analysis_path),
        "comment-signals-sha256": sha256(signals_path),
        "findings-sha256": sha256(findings_output),
    }
    document = build_html_document(
        findings,
        analysis_input,
        hashes["comment-signals-sha256"],
        hashes["analysis-input-sha256"],
        hashes["findings-sha256"],
    )
    hits = find_secret_hits(document)
    if hits:
        raise PipelineError("В HTML обнаружен секрет")
    html_path = outputs / "vk_content_report.html"
    html_path.write_text(document, encoding="utf-8")
    result = validate_html(html_path, analysis_input, hashes, findings)
    if not result["passed"]:
        raise PipelineError("; ".join(result["issues"]))
    return html_path


def validate_outputs(
    dataset_path: Path,
    comments_path: Path,
    xlsx_path: Path,
    final_validation_path: Path,
    run_dir: Path,
    media_root: Path | None = None,
) -> dict[str, Any]:
    chain = validate_source_chain(
        dataset_path,
        comments_path,
        xlsx_path,
        final_validation_path,
        media_root,
    )
    outputs = run_dir / "outputs"
    analysis_path = outputs / "analysis_input.json"
    signals_path = outputs / "comment-signals.json"
    findings_path = outputs / "report_findings.json"
    html_path = outputs / "vk_content_report.html"
    analysis_input = read_json(analysis_path)
    signals = read_json(signals_path)
    findings = read_json(findings_path)
    source = analysis_input.get("source", {})
    for key, expected in [
        ("dataset_sha256", chain["hashes"]["dataset"]),
        ("comments_sha256", chain["hashes"]["comments"]),
        ("xlsx_sha256", chain["hashes"]["xlsx"]),
        ("final_validation_sha256", chain["hashes"]["final_validation"]),
    ]:
        if source.get(key) != expected or signals.get("source", {}).get(key) != expected:
            raise PipelineError(f"Повреждена цепочка происхождения: {key}")
    if analysis_input.get("status") != signals.get("status"):
        raise PipelineError("Статусы analysis_input и comment-signals не совпадают")
    if findings.get("status") != analysis_input.get("status"):
        raise PipelineError("Статус findings не синхронизирован")
    validate_findings(findings, analysis_input)
    expected_hashes = {
        "analysis-input-sha256": sha256(analysis_path),
        "comment-signals-sha256": sha256(signals_path),
        "findings-sha256": sha256(findings_path),
    }
    html_result = validate_html(html_path, analysis_input, expected_hashes, findings)
    if not html_result["passed"]:
        raise PipelineError("; ".join(html_result["issues"]))
    result = {
        "passed": True,
        "status": analysis_input["status"],
        "source_hashes": chain["hashes"],
        "output_hashes": {
            "analysis_input": expected_hashes["analysis-input-sha256"],
            "comment_signals": expected_hashes["comment-signals-sha256"],
            "findings": expected_hashes["findings-sha256"],
            "html": sha256(html_path),
        },
        "counts": {
            "posts": len(analysis_input["posts"]),
            "comment_post_keys": len(chain["comments"]),
            "media_paths_checked": chain["media_paths_checked"],
            "rankable_relative_er_posts": analysis_input["quartiles"]["rankable_posts"],
            "quartile_size": analysis_input["quartiles"]["quartile_size"],
            "direct_posts": analysis_input["competitor_priority"]["counts"]["direct"],
            "indirect_posts": analysis_input["competitor_priority"]["counts"]["indirect"],
            "attention_posts": analysis_input["competitor_priority"]["counts"]["attention"],
            **html_result["counts"],
        },
        "secret_scan": html_result["secret_scan"],
    }
    write_json(outputs / "validation_report.json", result)
    return result


def confirm(run_dir: Path, confirmed_at: str) -> Path:
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})",
        confirmed_at,
    ):
        raise PipelineError("confirmed_at должен быть ISO-8601 с часовым поясом")
    outputs = run_dir / "outputs"
    for name in ["analysis_input.json", "comment-signals.json", "report_findings.json"]:
        path = outputs / name
        value = read_json(path)
        value["status"] = "confirmed"
        value["confirmed_at"] = confirmed_at
        value["confirmation_date"] = confirmed_at[:10]
        write_json(path, value)
    return build(run_dir, outputs / "report_findings.json")


def add_common_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--comments", type=Path, required=True)
    parser.add_argument("--xlsx", type=Path, required=True)
    parser.add_argument("--final-validation", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--media-root", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vk_content_report.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    add_common_input_arguments(prepare_parser)
    prepare_parser.add_argument("--top-n", type=int, default=3)
    prepare_parser.add_argument("--min-words", type=int, default=450)
    prepare_parser.add_argument("--max-words", type=int, default=1600)
    build_parser_ = subparsers.add_parser("build")
    build_parser_.add_argument("--run-dir", type=Path, required=True)
    build_parser_.add_argument("--findings", type=Path, required=True)
    validate_parser = subparsers.add_parser("validate")
    add_common_input_arguments(validate_parser)
    confirm_parser = subparsers.add_parser("confirm")
    confirm_parser.add_argument("--run-dir", type=Path, required=True)
    confirm_parser.add_argument("--confirmed-at", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(
                args.dataset,
                args.comments,
                args.xlsx,
                args.final_validation,
                args.run_dir,
                args.media_root,
                args.top_n,
                args.min_words,
                args.max_words,
            )
            print(json.dumps({key: str(value) for key, value in result.items()}, ensure_ascii=False))
        elif args.command == "build":
            print(build(args.run_dir, args.findings))
        elif args.command == "validate":
            print(
                json.dumps(
                    validate_outputs(
                        args.dataset,
                        args.comments,
                        args.xlsx,
                        args.final_validation,
                        args.run_dir,
                        args.media_root,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "confirm":
            print(confirm(args.run_dir, args.confirmed_at))
        return 0
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
