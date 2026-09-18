from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


class SkillError(RuntimeError):
    pass


class InputValidationError(SkillError):
    pass


class VkApiError(SkillError):
    pass


class MediaGateError(SkillError):
    pass


SECRET_PATTERNS = {
    "vk_token": re.compile(r"vk1\.[A-Za-z0-9_-]{20,}"),
    "access_token": re.compile(r"access_token\s*[=:]\s*[^\s,;&]+", re.I),
    "authorization_bearer": re.compile(r"authorization\s*:\s*bearer\s+\S+", re.I),
}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputValidationError(f"Invalid JSON file {path.name}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan_secrets_text(value: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(value)]


def scan_secret_files(paths: list[Path], literal_secret: str | None = None) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for path in paths:
        data = path.read_bytes()
        text = data.decode("utf-8", "ignore")
        for label in scan_secrets_text(text):
            hits.append({"file": path.name, "pattern": label})
        if literal_secret and literal_secret.encode("utf-8") in data:
            hits.append({"file": path.name, "pattern": "literal_secret"})
    return hits


def now_moscow() -> dt.datetime:
    return dt.datetime.now(ZoneInfo("Europe/Moscow")).replace(microsecond=0)


def parse_timestamp(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise InputValidationError("Timestamp must include timezone")
    return parsed


def normalize_moscow_timestamp(value: str | None = None) -> str:
    parsed = parse_timestamp(value) if value else now_moscow()
    return (
        parsed.astimezone(ZoneInfo("Europe/Moscow"))
        .replace(microsecond=0)
        .isoformat()
    )


def utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def validate_competitor_set(data: dict[str, Any], raw_text: str = "") -> list[dict[str, Any]]:
    if scan_secrets_text(raw_text):
        raise InputValidationError("competitor_set.json contains a secret-like value")
    if data.get("status") != "confirmed":
        raise InputValidationError("competitor_set.json must have status=confirmed")
    competitors = data.get("competitors")
    if not isinstance(competitors, list) or not competitors:
        raise InputValidationError("competitors must be a non-empty array")
    allowed_types = {"Прямой", "Косвенный", "За внимание"}
    seen: set[int] = set()
    normalized = []
    for index, item in enumerate(competitors):
        if not isinstance(item, dict):
            raise InputValidationError(f"competitors[{index}] must be an object")
        try:
            community_id = int(item["community_id"])
            followers = int(item["followers"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputValidationError(
                f"competitors[{index}] has invalid community_id/followers"
            ) from exc
        if community_id <= 0 or community_id in seen:
            raise InputValidationError(f"duplicate or invalid community_id: {community_id}")
        seen.add(community_id)
        name = str(item.get("name") or "").strip()
        vk = str(item.get("vk") or "").strip()
        competitor_type = str(item.get("competitor_type") or "").strip()
        parsed = urlparse(vk)
        vk_host = parsed.netloc.lower().split(":", 1)[0]
        if vk_host.startswith("www."):
            vk_host = vk_host[4:]
        if (
            not name
            or parsed.scheme not in {"http", "https"}
            or vk_host not in {"vk.com", "vk.ru", "m.vk.com", "m.vk.ru"}
        ):
            raise InputValidationError(f"competitors[{index}] has invalid name/VK URL")
        if competitor_type not in allowed_types:
            raise InputValidationError(f"invalid competitor type: {competitor_type}")
        normalized.append(
            {
                "community_id": community_id,
                "name": name,
                "vk": vk,
                "competitor_type": competitor_type,
                "followers": followers,
            }
        )
    return normalized
