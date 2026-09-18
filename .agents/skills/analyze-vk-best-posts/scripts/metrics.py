from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Callable

from common import InputValidationError


CLIP_TYPES = {"clip", "short_video"}
STRUCTURED_CAROUSEL_TYPES = {
    "carousel",
    "pretty_cards",
    "photos_list",
    "album",
    "market_album",
}


def calculate_er(likes: int, comments: int, reposts: int, views: int | None) -> float | None:
    if views is None or int(views) <= 0:
        return None
    return (int(likes) + int(comments) + int(reposts)) / int(views)


def deduplicate_posts(posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicates = 0
    for post in posts:
        key = f"{int(post['owner_id'])}_{int(post['id'])}"
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        result.append(post)
    return result, duplicates


def filter_fixed_window(
    posts: list[dict[str, Any]], start_ts: int, end_ts: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    selected = []
    old_pinned = 0
    future = 0
    for post in posts:
        timestamp = int(post.get("date") or 0)
        if timestamp < start_ts:
            if post.get("is_pinned"):
                old_pinned += 1
            continue
        if timestamp > end_ts:
            future += 1
            continue
        selected.append(post)
    return selected, {"old_pinned_skipped": old_pinned, "future_skipped": future}


def verify_video_attachment(
    attachment: dict[str, Any],
    resolver: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    outer_type = str(attachment.get("type") or "").lower()
    video = dict(
        attachment.get("video")
        or attachment.get(outer_type)
        or {}
    )
    inner_type = str(video.get("type") or "").lower()
    source = "attachments.video.type"
    if not inner_type and resolver:
        resolved = resolver(video)
        if resolved:
            video.update(resolved)
            inner_type = str(video.get("type") or "").lower()
            source = "video.get"
        else:
            source = "video.get_unavailable"
    is_clip = outer_type in CLIP_TYPES or inner_type in CLIP_TYPES
    if is_clip:
        verification_status = "confirmed_clip"
    elif inner_type:
        verification_status = "confirmed_video"
    else:
        verification_status = "unavailable"
    return {
        "outer_type": outer_type,
        "inner_type": inner_type or None,
        "is_clip": is_clip,
        "verification_status": verification_status,
        "verification_source": source,
        "owner_id": int(video.get("owner_id") or 0),
        "video_id": int(video.get("id") or 0),
        "access_key": video.get("access_key"),
        "files": video.get("files") or {},
        "player": video.get("player"),
        "image": video.get("image") or video.get("first_frame") or [],
        "duration": video.get("duration"),
    }


def classify_post(
    attachments: list[dict[str, Any]], verified_videos: list[dict[str, Any]]
) -> str:
    if any(item.get("verification_status") == "confirmed_clip" for item in verified_videos):
        return "Клип"
    if any(item.get("verification_status") == "confirmed_video" for item in verified_videos):
        return "Видео"
    if any(item.get("verification_status") == "unavailable" for item in verified_videos):
        raise InputValidationError(
            "Video attachment type could not be confirmed through attachments or video.get"
        )
    outer_types = [str(item.get("type") or "").lower() for item in attachments]
    photo_count = sum(1 for item in attachments if item.get("type") == "photo")
    if photo_count >= 2 or any(item in STRUCTURED_CAROUSEL_TYPES for item in outer_types):
        return "Карусель"
    if photo_count == 1:
        return "Картинка"
    if not attachments:
        return "Текстовый пост"
    raise InputValidationError("У поста есть вложения, но основной формат не поддерживается")


def poll_flag(attachments: list[dict[str, Any]]) -> bool:
    return any(item.get("type") == "poll" for item in attachments)


def giveaway_flag(text: str) -> tuple[bool, list[str]]:
    normalized = text.lower()
    contest = bool(
        re.search(r"\b(розыгрыш|конкурс|разыгра(?:ем|ют)|выигра(?:й|ть)|подарок)\b", normalized)
    )
    active = bool(
        re.search(
            r"(участв|услови|сделай|выполн|подпиш|репост|постав.{0,12}лайк|"
            r"напиш.{0,12}коммент|остав.{0,12}коммент|отмет|разыграем|выиграй)",
            normalized,
        )
    )
    checks = {
        "conditions_or_mechanics": r"(услови|подпис|репост|лайк|коммент|отмет|участв|выполн)",
        "deadline_or_draw_date": r"(до\s+\d|итог|результат|\d{1,2}[./]\d{1,2})",
        "winner_or_prize": r"(победител|приз|сертификат|билет|абонемент|подарок|бесплатн)",
    }
    evidence = [name for name, pattern in checks.items() if re.search(pattern, normalized)]
    return bool(contest and active and len(evidence) >= 2), evidence


def apply_benchmarks(posts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for post in posts:
        grouped[str(post["community_id"])].append(post)
    benchmarks: dict[str, dict[str, Any]] = {}
    for community_id, items in grouped.items():
        valid = [float(item["er"]) for item in items if item.get("er") is not None]
        benchmark = sum(valid) / len(valid) if valid else None
        benchmarks[community_id] = {
            "benchmark": benchmark,
            "valid_post_count": len(valid),
            "window_post_count": len(items),
        }
        for item in items:
            item["benchmark"] = benchmark
            item["is_best"] = bool(
                benchmark is not None
                and item.get("er") is not None
                and float(item["er"]) >= benchmark
            )
            item["result"] = (
                "Лучший пост"
                if item["is_best"]
                else ("ER не рассчитан" if item.get("er") is None else "Ниже бенчмарка")
            )
    return benchmarks
