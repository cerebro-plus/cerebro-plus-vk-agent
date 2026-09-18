#!/usr/bin/env python3
"""Create, synchronize, confirm, and validate business-context artifacts."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = SKILL_ROOT / "references" / "business-context.schema.json"
JSON_NAME = "business_context.json"
MARKDOWN_NAME = "business_card.md"
APPROVAL_NAME = "business_approval.json"
CURRENT_SCHEMA_VERSION = "1.1"
LEGACY_SCHEMA_VERSION = "1.0"
DIRECT_RULE = (
    "Продают тот же продукт той же аудитории; ассортимент может быть шире, "
    "но должен включать большинство основных позиций, а основная специализация "
    "должна совпадать."
)
INDIRECT_RULE = "Продают часть ассортимента бизнеса либо похожий ассортимент."
ATTENTION_RULE = (
    "Закрывают схожую потребность и могут получить покупку того же клиента "
    "одновременно с покупкой у бизнеса или в дополнение к ней."
)
REQUIRED_CLASSIFICATION_FACTORS = {
    "Ассортимент",
    "Аудитория",
    "Потребности клиентов",
    "География",
    "Цены",
    "Формат продаж",
    "Производство или перепродажа",
    "Онлайн- и офлайн-формат",
}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

SECRET_PATTERNS = (
    re.compile(r"vk1\.[A-Za-z0-9._-]{20,}"),
    re.compile(
        r"(?i)(access[_-]?token|api[_-]?key|client[_-]?secret|password|token)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


class ContextError(Exception):
    """Raised for invalid input or unsafe artifacts."""


def today_iso() -> str:
    return dt.date.today().isoformat()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContextError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContextError(
            f"Invalid JSON in {path.name}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(value, dict):
        raise ContextError("Business context root must be a JSON object.")
    return value


def load_schema() -> dict[str, Any]:
    return load_json(SCHEMA_PATH)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise ContextError(f"Supporting file not found: {path}") from exc
    return digest.hexdigest()


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def resolve_ref(root_schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ContextError(f"Unsupported schema reference: {reference}")
    current: Any = root_schema
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        current = current[key]
    if not isinstance(current, dict):
        raise ContextError(f"Schema reference is not an object: {reference}")
    return current


def type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def validate_format(value: str, format_name: str, path: str) -> list[str]:
    if format_name == "date":
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError:
            return [f"{path}: expected ISO date YYYY-MM-DD"]
        if parsed.isoformat() != value:
            return [f"{path}: expected canonical ISO date YYYY-MM-DD"]
    elif format_name == "uri":
        parsed_url = urlparse(value)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return [f"{path}: expected absolute HTTP(S) URL"]
    return []


def validate_schema_node(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    if "$ref" in schema:
        return validate_schema_node(value, resolve_ref(root_schema, schema["$ref"]), root_schema, path)

    if "anyOf" in schema:
        branch_errors = [
            validate_schema_node(value, branch, root_schema, path)
            for branch in schema["anyOf"]
        ]
        if any(not errors for errors in branch_errors):
            return []
        return [f"{path}: value does not match any allowed schema"]

    errors: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(type_matches(value, item) for item in allowed):
            return [f"{path}: expected type {' or '.join(allowed)}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant value {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in the allowed set")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string is shorter than required")
        if "format" in schema:
            errors.extend(validate_format(value, schema["format"], path))

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: array has fewer items than required")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                errors.extend(
                    validate_schema_node(item, item_schema, root_schema, f"{path}[{index}]")
                )

    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                errors.append(f"{path}.{key}: required field is missing")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                errors.extend(
                    validate_schema_node(item, properties[key], root_schema, f"{path}.{key}")
                )
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}.{key}: unexpected field")

    return errors


def secret_findings(text: str, label: str) -> list[str]:
    findings: list[str] = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(f"{label}: potential secret detected ({pattern.pattern[:28]}...)")
    return findings


def validate_business_rules(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    schema_version = data.get("schema_version")
    status = data.get("status")
    confirmation_date = data.get("confirmation_date")
    if status == "draft" and confirmation_date is not None:
        errors.append("$.confirmation_date: draft context must use null")
    if status == "confirmed" and not confirmation_date:
        errors.append("$.confirmation_date: confirmed context requires a date")

    sources = data.get("sources", {})
    online = data.get("online_presence", {})
    vk_source = sources.get("vk") if isinstance(sources, dict) else None
    if not isinstance(vk_source, dict):
        errors.append("$.sources.vk: VK source is required")
    elif vk_source.get("url") != online.get("vk_url"):
        errors.append("$.sources.vk.url: must match $.online_presence.vk_url")

    website_source = sources.get("website") if isinstance(sources, dict) else None
    website_url = online.get("website_url") if isinstance(online, dict) else None
    if website_source is None and website_url is not None:
        errors.append("$.sources.website: website source is required when website_url is set")
    if isinstance(website_source, dict) and website_source.get("url") != website_url:
        errors.append("$.sources.website.url: must match $.online_presence.website_url")

    try:
        constraints = data["competitors"]["search_constraints"]
        target_count = constraints["target_count"]
        freshness_days = constraints["freshness_days"]
        minimum = constraints["audience_size"]["min"]
        maximum = constraints["audience_size"]["max"]
        if target_count <= 0:
            errors.append("$.competitors.search_constraints.target_count: must be positive")
        if freshness_days <= 0:
            errors.append("$.competitors.search_constraints.freshness_days: must be positive")
        if minimum is not None and minimum < 0:
            errors.append("$.competitors.search_constraints.audience_size.min: cannot be negative")
        if maximum is not None and maximum < 0:
            errors.append("$.competitors.search_constraints.audience_size.max: cannot be negative")
        if minimum is not None and maximum is not None and minimum > maximum:
            errors.append("$.competitors.search_constraints.audience_size: min exceeds max")
    except (KeyError, TypeError):
        pass

    for index, offer in enumerate(data.get("pricing", {}).get("offers", [])):
        amount = offer.get("amount")
        if amount is not None and amount < 0:
            errors.append(f"$.pricing.offers[{index}].amount: cannot be negative")

    if schema_version == CURRENT_SCHEMA_VERSION:
        source_reviews = data.get("source_reviews")
        if not isinstance(source_reviews, list) or not source_reviews:
            errors.append("$.source_reviews: required for schema 1.1")
            source_reviews = []
        review_urls: set[str] = set()
        vk_reviews = 0
        for index, review in enumerate(source_reviews):
            if not isinstance(review, dict):
                continue
            review_urls.update(
                str(value)
                for value in (review.get("url"), review.get("final_url"))
                if value
            )
            if review.get("kind") == "vk":
                vk_reviews += 1
            if review.get("status") in {"partial", "failed"}:
                if review.get("limitations_accepted") is not True and status == "confirmed":
                    errors.append(
                        f"$.source_reviews[{index}].limitations_accepted: "
                        "required before confirmation"
                    )
                if not review.get("limitations"):
                    errors.append(
                        f"$.source_reviews[{index}].limitations: "
                        "partial or failed source needs a reason"
                    )
        if vk_reviews == 0:
            errors.append("$.source_reviews: at least one VK source is required")
        if isinstance(vk_source, dict) and vk_source.get("url") not in review_urls:
            errors.append("$.sources.vk.url: source was not recorded in source_reviews")
        additional_sources = sources.get("additional") if isinstance(sources, dict) else None
        if not isinstance(additional_sources, list):
            errors.append("$.sources.additional: required for schema 1.1")
        else:
            for index, source in enumerate(additional_sources):
                if isinstance(source, dict) and source.get("url") not in review_urls:
                    errors.append(
                        f"$.sources.additional[{index}].url: "
                        "source was not recorded in source_reviews"
                    )

        interview = data.get("interview")
        if not isinstance(interview, dict):
            errors.append("$.interview: required for schema 1.1")
        else:
            if interview.get("business_question_limit") != 15:
                errors.append("$.interview.business_question_limit: must equal 15")
            business_count = interview.get("business_questions_asked")
            clarification_count = interview.get("clarification_questions_asked")
            competitor_count = interview.get("competitor_questions_asked")
            if (
                not isinstance(business_count, int)
                or business_count < 0
                or business_count > 15
            ):
                errors.append(
                    "$.interview.business_questions_asked: must be between 0 and 15"
                )
            if not isinstance(clarification_count, int) or clarification_count < 0:
                errors.append("$.interview.clarification_questions_asked: invalid")
            if not isinstance(competitor_count, int) or competitor_count < 0:
                errors.append("$.interview.competitor_questions_asked: invalid")
            if interview.get("additional_round_offered") is not True:
                errors.append("$.interview.additional_round_offered: must be true")
            if not SHA256_RE.fullmatch(
                str(interview.get("source_review_sha256") or "")
            ):
                errors.append("$.interview.source_review_sha256: invalid SHA-256")

        competitors = data.get("competitors")
        if not isinstance(competitors, dict):
            competitors = {}
        rules = competitors.get("classification_rules")
        expected_rules = {
            "direct": DIRECT_RULE,
            "indirect": INDIRECT_RULE,
            "attention": ATTENTION_RULE,
        }
        if rules != expected_rules:
            errors.append(
                "$.competitors.classification_rules: "
                "must use the project definitions without weakening"
            )
        factors = competitors.get("classification_factors")
        if not isinstance(factors, list) or not REQUIRED_CLASSIFICATION_FACTORS.issubset(
            set(factors)
        ):
            errors.append(
                "$.competitors.classification_factors: "
                "missing mandatory comparison factors"
            )
        key_features = competitors.get("key_features")
        if not isinstance(key_features, list) or not key_features:
            errors.append("$.competitors.key_features: non-empty array is required")
        policy = competitors.get("matching_policy")
        if not isinstance(policy, dict):
            errors.append("$.competitors.matching_policy: required for schema 1.1")
        else:
            applies_to = policy.get("applies_to")
            if not isinstance(applies_to, list) or not applies_to:
                errors.append("$.competitors.matching_policy.applies_to: cannot be empty")
            must_repeat = policy.get("must_repeat_in_competitors")
            importance = policy.get("importance")
            if must_repeat is True and importance != "required":
                errors.append(
                    "$.competitors.matching_policy.importance: "
                    "must be required when feature repetition is mandatory"
                )
            if must_repeat is True and isinstance(key_features, list):
                inclusions = (
                    competitors.get("search_constraints", {}).get(
                        "required_inclusions", []
                    )
                    if isinstance(competitors.get("search_constraints"), dict)
                    else []
                )
                inclusion_text = " ".join(str(item).casefold() for item in inclusions)
                missing_features = [
                    feature
                    for feature in key_features
                    if str(feature).casefold() not in inclusion_text
                ]
                if missing_features:
                    errors.append(
                        "$.competitors.search_constraints.required_inclusions: "
                        "mandatory key features are missing"
                    )
    return errors


def validate_context(data: dict[str, Any]) -> list[str]:
    schema = load_schema()
    errors = validate_schema_node(data, schema, schema)
    errors.extend(validate_business_rules(data))
    serialized = canonical_json(data)
    errors.extend(secret_findings(serialized, JSON_NAME))
    return sorted(set(errors))


def text(value: Any) -> str:
    if value is None:
        return "не указано"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def bullets(items: list[str], empty: str = "Не указано") -> list[str]:
    return [f"- {item}" for item in items] if items else [f"- {empty}"]


def escape_table(value: Any) -> str:
    return text(value).replace("|", "\\|").replace("\n", " ")


def amount_text(value: Any) -> str:
    if value is None:
        return "не указана"
    return f"{value:,}".replace(",", " ")


def render_markdown(data: dict[str, Any]) -> str:
    business = data["business"]
    assortment = data["assortment"]
    production = data["production"]
    audience = data["audience"]
    geography = data["geography"]
    pricing = data["pricing"]
    sales = data["sales"]
    acquisition = data["acquisition"]
    online = data["online_presence"]
    competitors = data["competitors"]
    constraints = competitors["search_constraints"]
    lines: list[str] = [
        f"# Карточка бизнеса: {business['name']}",
        "",
        f"**Статус:** `{data['status']}`  ",
        f"**Дата формирования:** {data['created_at']}  ",
        f"**Дата обновления:** {data['updated_at']}  ",
        f"**Дата подтверждения:** {text(data['confirmation_date'])}",
        "",
        "## Описание бизнеса",
        "",
        business["description"],
        "",
        "## Ниша и позиционирование",
        "",
        f"- Основная ниша: {business['marketing_niche']}",
        f"- Более широкие категории: {', '.join(business['broader_categories']) or 'не указаны'}",
        f"- Позиционирование: {business['positioning']}",
        f"- Главное отличие: {business['main_differentiator']}",
        "",
        "## Ассортимент",
        "",
        f"**Ширина:** {assortment['breadth']}",
        "",
        "**Различия между предложениями:**",
        "",
        *bullets(assortment["variation_dimensions"]),
        "",
        "### Основные предложения",
        "",
    ]
    for offer in assortment["primary_offers"]:
        lines.append(
            f"- **{offer['name']}** — {', '.join(offer['formats'])}; приоритет: {offer['priority']}."
        )
    lines.extend(["", "### Второстепенные предложения", "", *bullets(assortment["secondary_offers"])])
    lines.extend(["", "### Продуктовые категории", ""])
    for category in assortment["product_categories"]:
        examples = ", ".join(category["examples"]) or "примеры не указаны"
        lines.append(f"- **{category['name']}** ({category['role']}): {examples}.")

    lines.extend(
        [
            "",
            "## Производство",
            "",
            f"- Модель: {production['model']}",
            f"- Создатели или поставщики: {production['creators_or_suppliers']}",
            f"- Срок: {text(production['lead_time'])}",
            f"- Примечания: {text(production['notes'])}",
            "",
            "**Процесс:**",
            "",
            *bullets(production["process"]),
            "",
            "**Возможности:**",
            "",
            *bullets(production["capabilities"]),
            "",
            "## Аудитория",
            "",
            "**Основные плательщики:**",
            "",
            *bullets(audience["primary_payers"]),
            "",
        ]
    )
    for segment in audience["segments"]:
        lines.extend(
            [
                f"### {segment['name']}",
                "",
                *bullets(segment["tasks"]),
                "",
            ]
        )

    lines.extend(
        [
            "## География и формат",
            "",
            f"- Основная география: {geography['primary']}",
            f"- Дополнительная география: {', '.join(geography['additional']) or 'не указана'}",
            f"- Формат продаж и оказания услуг: {', '.join(geography['sales_format'])}",
            "",
            "## Цены",
            "",
            f"- Валюта: {pricing['currency']}",
            f"- Структура: {pricing['structure']}",
            "",
            "| Предложение | Цена | Единица | Условия | Подтверждено |",
            "|---|---:|---|---|---|",
        ]
    )
    for offer in pricing["offers"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_table(offer["name"]),
                    escape_table(amount_text(offer["amount"])),
                    escape_table(offer["unit"]),
                    escape_table(offer["conditions"]),
                    "да" if offer["confirmed"] else "нет",
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Заказ, оплата, доступ и доставка",
            "",
            f"- Каналы бронирования: {', '.join(sales['booking_channels'])}",
            f"- Порядок заказа: {sales['order_process']}",
            f"- Предоплата: {text(sales['prepayment_required'])}",
            f"- Момент оплаты: {sales['payment_timing']}",
            f"- Способы оплаты: {', '.join(sales['payment_methods']) or 'не указаны'}",
            f"- Доступ или доставка: {sales['access_or_delivery']}",
            "",
            "## Привлечение",
            "",
            f"- Основные каналы: {', '.join(acquisition['primary_channels'])}",
            f"- Дополнительные каналы: {', '.join(acquisition['additional_channels']) or 'не указаны'}",
            "",
            "## Интернет-площадки",
            "",
            f"- VK: {online['vk_url']}",
            f"- Сайт: {text(online['website_url'])}",
        ]
    )
    for link in online["other_links"]:
        lines.append(f"- {link['label']}: {link['url']}")

    lines.extend(["", "## Преимущества", "", *bullets(data["advantages"])])
    lines.extend(["", "## Ограничения", "", *bullets(data["limitations"])])
    lines.extend(["", "## Известные конкуренты", ""])
    if competitors["known"]:
        for item in competitors["known"]:
            lines.append(
                f"- **{item['name']}** — тип: {text(item['type'])}; "
                f"ссылка: {text(item['url'])}; примечания: {text(item['notes'])}."
            )
    else:
        lines.append("- Не указаны.")

    key_features = competitors.get("key_features", [])
    matching_policy = competitors.get("matching_policy")
    lines.extend(["", "## Ключевые особенности и критерий совпадения", ""])
    lines.extend(bullets(key_features))
    if isinstance(matching_policy, dict):
        lines.extend(
            [
                "",
                f"- Обязательное повторение: "
                f"{text(matching_policy['must_repeat_in_competitors'])}",
                f"- Важность: {matching_policy['importance']}",
                f"- Применяется к типам: "
                f"{', '.join(matching_policy['applies_to'])}",
                f"- Обоснование: {matching_policy['rationale']}",
            ]
        )
    else:
        lines.extend(["", "- Политика совпадения не зафиксирована."])

    rules = competitors["classification_rules"]
    lines.extend(
        [
            "",
            "## Правила классификации конкурентов",
            "",
            f"- Прямые: {rules['direct']}",
            f"- Косвенные: {rules['indirect']}",
            f"- За внимание: {rules['attention']}",
            f"- Факторы: {', '.join(competitors['classification_factors'])}",
            "",
            "## Ограничения поиска конкурентов",
            "",
            f"- Площадки: {', '.join(constraints['platforms'])}",
            f"- Количество: {constraints['target_count']}",
            f"- География: {constraints['geography']}",
            "- Размер аудитории: "
            f"от {text(constraints['audience_size']['min'])} "
            f"до {text(constraints['audience_size']['max'])}",
            f"- Свежесть публикаций: {constraints['freshness_days']} дней",
            f"- Обязательные включения: {', '.join(constraints['required_inclusions']) or 'нет'}",
            f"- Исключения: {', '.join(constraints['exclusions']) or 'нет'}",
            "",
            "## Предполагаемые поисковые запросы",
            "",
            *bullets(competitors["proposed_search_queries"]),
            "",
            "## Оставшиеся пробелы",
            "",
            *bullets(data["remaining_gaps"], "Пробелов не зафиксировано"),
            "",
            "## Источники",
            "",
        ]
    )
    vk_source = data["sources"]["vk"]
    lines.extend(
        [
            f"- VK: {vk_source['url']}",
            f"  - Дата получения: {vk_source['retrieved_at']}",
            f"  - Метод: {vk_source['method']}",
            f"  - Охват: {', '.join(vk_source['coverage'])}",
            f"  - Ограничения: {', '.join(vk_source['limitations']) or 'нет'}",
        ]
    )
    website_source = data["sources"]["website"]
    if website_source is None:
        lines.append("- Сайт: отсутствует.")
    else:
        lines.extend(
            [
                f"- Сайт: {website_source['url']}",
                f"  - Дата получения: {website_source['retrieved_at']}",
                f"  - Метод: {website_source['method']}",
                f"  - Охват: {', '.join(website_source['coverage'])}",
                f"  - Ограничения: {', '.join(website_source['limitations']) or 'нет'}",
            ]
        )
    for source in data["sources"].get("additional", []):
        lines.extend(
            [
                f"- Дополнительный источник: {source['url']}",
                f"  - Дата получения: {source['retrieved_at']}",
                f"  - Метод: {source['method']}",
                f"  - Охват: {', '.join(source['coverage'])}",
                f"  - Ограничения: {', '.join(source['limitations']) or 'нет'}",
            ]
        )

    source_reviews = data.get("source_reviews", [])
    if source_reviews:
        lines.extend(["", "## Проверка исходных ссылок", ""])
        for review in source_reviews:
            lines.append(
                f"- **{review['label']}** — {review['status']}; "
                f"{review['method']}; {review['url']}."
            )
            lines.append(
                f"  - Охват: {', '.join(review['coverage']) or 'нет'}"
            )
            lines.append(
                f"  - Ограничения: {', '.join(review['limitations']) or 'нет'}"
            )
            lines.append(
                "  - Ограничения приняты пользователем: "
                + text(review["limitations_accepted"])
            )

    interview = data.get("interview")
    if isinstance(interview, dict):
        lines.extend(
            [
                "",
                "## Параметры интервью",
                "",
                f"- Основных вопросов: "
                f"{interview['business_questions_asked']} из "
                f"{interview['business_question_limit']}",
                f"- Уточнений: {interview['clarification_questions_asked']}",
                f"- Вопросов конкурентного блока: "
                f"{interview['competitor_questions_asked']}",
                f"- Дополнительный раунд предложен: "
                f"{text(interview['additional_round_offered'])}",
                f"- Дополнительный раунд запрошен: "
                f"{text(interview['additional_round_requested'])}",
                f"- Закрытые темы: "
                f"{', '.join(interview['resolved_topics']) or 'не указаны'}",
            ]
        )
    lines.extend(["", "Секреты в карточку не включены.", ""])
    return "\n".join(lines)


def attach_supporting_data(
    data: dict[str, Any],
    source_review_path: Path | None,
    interview_state_path: Path | None,
) -> dict[str, Any]:
    prepared = copy.deepcopy(data)
    if source_review_path is None and interview_state_path is None:
        return prepared
    if source_review_path is None or interview_state_path is None:
        raise ContextError(
            "Schema 1.1 requires both --source-review and --interview-state."
        )

    source_review = load_json(source_review_path)
    if source_review.get("status") not in {"collected", "partial"}:
        raise ContextError(
            "source_review.json must be collected or partial before artifact creation."
        )
    if secret_findings(canonical_json(source_review), source_review_path.name):
        raise ContextError("source_review.json contains a potential secret.")
    source_items = source_review.get("sources")
    if not isinstance(source_items, list) or not source_items:
        raise ContextError("source_review.json has no sources.")

    existing_acceptance: dict[str, bool] = {}
    for item in prepared.get("source_reviews", []):
        if isinstance(item, dict) and item.get("url"):
            existing_acceptance[str(item["url"])] = (
                item.get("limitations_accepted") is True
            )
    reviews: list[dict[str, Any]] = []
    for item in source_items:
        if not isinstance(item, dict):
            raise ContextError("source_review.json contains an invalid source.")
        limitations = item.get("limitations")
        if not isinstance(limitations, list):
            raise ContextError("source_review.json source limitations must be an array.")
        url = str(item.get("url") or "")
        reviews.append(
            {
                "url": url,
                "final_url": str(item.get("final_url") or url),
                "label": str(item.get("label") or url),
                "kind": item.get("kind"),
                "status": item.get("status"),
                "retrieved_at": item.get("retrieved_at"),
                "method": item.get("method"),
                "coverage": item.get("coverage") or [],
                "limitations": limitations,
                "limitations_accepted": (
                    True
                    if not limitations
                    else existing_acceptance.get(url, False)
                ),
            }
        )
    prepared["source_reviews"] = reviews

    interview_state = load_json(interview_state_path)
    if (
        interview_state.get("status") != "complete"
        or interview_state.get("phase") != "complete"
    ):
        raise ContextError("interview_state.json must be complete.")
    if secret_findings(canonical_json(interview_state), interview_state_path.name):
        raise ContextError("interview_state.json contains a potential secret.")
    source_digest = sha256_file(source_review_path)
    if interview_state.get("source_review_sha256") != source_digest:
        raise ContextError(
            "interview_state.json does not match the supplied source_review.json."
        )
    business_state = interview_state.get("business")
    if not isinstance(business_state, dict):
        raise ContextError("interview_state.json has no business summary.")
    prepared["interview"] = {
        "business_question_limit": interview_state.get("business_question_limit"),
        "business_questions_asked": interview_state.get(
            "business_questions_asked"
        ),
        "competitor_questions_asked": interview_state.get(
            "competitor_questions_asked"
        ),
        "clarification_questions_asked": interview_state.get(
            "clarification_questions_asked"
        ),
        "additional_round_offered": business_state.get(
            "additional_round_offered"
        ),
        "additional_round_requested": business_state.get(
            "additional_round_requested"
        ),
        "resolved_topics": interview_state.get("resolved_topics") or [],
        "source_review_sha256": source_digest,
    }
    prepared["remaining_gaps"] = business_state.get("remaining_gaps") or []
    return prepared


def normalize_draft(data: dict[str, Any], created_at: str | None = None) -> dict[str, Any]:
    normalized = copy.deepcopy(data)
    current_date = today_iso()
    normalized["schema_version"] = normalized.get(
        "schema_version", LEGACY_SCHEMA_VERSION
    )
    normalized["status"] = "draft"
    normalized["created_at"] = normalized.get("created_at") or created_at or current_date
    normalized["updated_at"] = current_date
    normalized["confirmation_date"] = None
    normalized["security"] = {"contains_secrets": False}
    if normalized["schema_version"] == CURRENT_SCHEMA_VERSION:
        normalized.setdefault("source_reviews", [])
        normalized.setdefault("interview", {})
        normalized.setdefault("sources", {}).setdefault("additional", [])
    return normalized


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_artifacts(data: dict[str, Any], directory: Path) -> None:
    errors = validate_context(data)
    markdown = render_markdown(data)
    errors.extend(secret_findings(markdown, MARKDOWN_NAME))
    if errors:
        raise ContextError("\n".join(sorted(set(errors))))
    atomic_write(directory / JSON_NAME, canonical_json(data))
    atomic_write(directory / MARKDOWN_NAME, markdown)


def create_artifacts(
    input_path: Path,
    output_dir: Path,
    source_review_path: Path | None = None,
    interview_state_path: Path | None = None,
) -> None:
    if (output_dir / JSON_NAME).exists() or (output_dir / MARKDOWN_NAME).exists():
        raise ContextError("Output artifacts already exist; use a new run directory.")
    data = attach_supporting_data(
        load_json(input_path),
        source_review_path,
        interview_state_path,
    )
    data = normalize_draft(data)
    write_artifacts(data, output_dir)


def sync_artifacts(
    input_path: Path,
    directory: Path,
    source_review_path: Path | None = None,
    interview_state_path: Path | None = None,
) -> None:
    json_path = directory / JSON_NAME
    markdown_path = directory / MARKDOWN_NAME
    if not json_path.exists() or not markdown_path.exists():
        raise ContextError("Both existing artifacts are required for sync.")
    existing = load_json(json_path)
    data = attach_supporting_data(
        load_json(input_path),
        source_review_path,
        interview_state_path,
    )
    data = normalize_draft(data, created_at=existing.get("created_at"))
    write_artifacts(data, directory)


def record_approval(
    directory: Path,
    statement: str,
    approval_date: str | None = None,
    replace: bool = False,
) -> None:
    errors = validate_directory(directory)
    if errors:
        raise ContextError("Cannot approve invalid artifacts:\n" + "\n".join(errors))
    data = load_json(directory / JSON_NAME)
    if data.get("status") != "draft":
        raise ContextError("Only a draft card can be approved.")
    cleaned_statement = " ".join(statement.split())
    if not cleaned_statement:
        raise ContextError("Explicit approval statement must not be empty.")
    if secret_findings(cleaned_statement, "approval statement"):
        raise ContextError("Approval statement contains a potential secret.")
    date_value = approval_date or today_iso()
    if validate_format(date_value, "date", "--date"):
        raise ContextError("--date must use YYYY-MM-DD.")
    approval_path = directory / APPROVAL_NAME
    if approval_path.exists() and not replace:
        raise ContextError(
            "Approval record already exists; explicit --replace is required."
        )
    approval = {
        "schema_version": "1.0",
        "approved": True,
        "approved_at": date_value,
        "statement": cleaned_statement,
        "draft_hashes": {
            JSON_NAME: sha256_file(directory / JSON_NAME),
            MARKDOWN_NAME: sha256_file(directory / MARKDOWN_NAME),
        },
        "security": {"contains_secrets": False},
    }
    atomic_write(approval_path, canonical_json(approval))


def validate_approval(directory: Path) -> dict[str, Any]:
    approval_path = directory / APPROVAL_NAME
    approval = load_json(approval_path)
    if approval.get("schema_version") != "1.0" or approval.get("approved") is not True:
        raise ContextError("Approval record is invalid.")
    if approval.get("security") != {"contains_secrets": False}:
        raise ContextError("Approval record has an unsafe security declaration.")
    if secret_findings(canonical_json(approval), APPROVAL_NAME):
        raise ContextError("Approval record contains a potential secret.")
    hashes = approval.get("draft_hashes")
    if not isinstance(hashes, dict):
        raise ContextError("Approval record has no draft hashes.")
    for name in (JSON_NAME, MARKDOWN_NAME):
        expected = hashes.get(name)
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise ContextError(f"Approval record has invalid hash for {name}.")
        if sha256_file(directory / name) != expected:
            raise ContextError(
                "Draft changed after approval; show it again and record new approval."
            )
    date_value = approval.get("approved_at")
    if not isinstance(date_value, str) or validate_format(
        date_value, "date", "approved_at"
    ):
        raise ContextError("Approval record has an invalid date.")
    return approval


def confirm_artifacts(directory: Path, confirmation_date: str | None = None) -> None:
    existing_errors = validate_directory(directory)
    if existing_errors:
        raise ContextError("Cannot confirm invalid artifacts:\n" + "\n".join(existing_errors))
    data = load_json(directory / JSON_NAME)
    approval = None
    if data.get("schema_version") == CURRENT_SCHEMA_VERSION:
        approval = validate_approval(directory)
    date_value = confirmation_date or (
        approval["approved_at"] if approval is not None else today_iso()
    )
    if validate_format(date_value, "date", "--date"):
        raise ContextError("--date must use YYYY-MM-DD.")
    if approval is not None and date_value != approval["approved_at"]:
        raise ContextError("Confirmation date must match the approval date.")
    data["status"] = "confirmed"
    data["confirmation_date"] = date_value
    data["updated_at"] = today_iso()
    write_artifacts(data, directory)


def validate_directory(directory: Path) -> list[str]:
    json_path = directory / JSON_NAME
    markdown_path = directory / MARKDOWN_NAME
    errors: list[str] = []
    if not json_path.exists():
        errors.append(f"{JSON_NAME}: file is missing")
    if not markdown_path.exists():
        errors.append(f"{MARKDOWN_NAME}: file is missing")
    if errors:
        return errors

    try:
        json_text = json_path.read_text(encoding="utf-8")
        data = json.loads(json_text)
    except UnicodeDecodeError:
        return [f"{JSON_NAME}: file is not valid UTF-8"]
    except json.JSONDecodeError as exc:
        return [
            f"{JSON_NAME}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ]
    if not isinstance(data, dict):
        return [f"{JSON_NAME}: root must be an object"]

    try:
        markdown_text = markdown_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return [f"{MARKDOWN_NAME}: file is not valid UTF-8"]

    errors.extend(validate_context(data))
    errors.extend(secret_findings(markdown_text, MARKDOWN_NAME))
    if json_text != canonical_json(data):
        errors.append(f"{JSON_NAME}: formatting is not canonical")
    expected_markdown = render_markdown(data)
    if markdown_text != expected_markdown:
        errors.append(
            f"{MARKDOWN_NAME}: out of sync with {JSON_NAME}; run the sync command"
        )
    return sorted(set(errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new draft artifact pair.")
    create_parser.add_argument("--input", required=True, type=Path)
    create_parser.add_argument("--output-dir", required=True, type=Path)
    create_parser.add_argument("--source-review", type=Path)
    create_parser.add_argument("--interview-state", type=Path)

    sync_parser = subparsers.add_parser("sync", help="Synchronize an existing pair as draft.")
    sync_parser.add_argument("--input", required=True, type=Path)
    sync_parser.add_argument("--directory", required=True, type=Path)
    sync_parser.add_argument("--source-review", type=Path)
    sync_parser.add_argument("--interview-state", type=Path)

    approval_parser = subparsers.add_parser(
        "approve",
        help="Record explicit approval for the current draft hashes.",
    )
    approval_parser.add_argument("--directory", required=True, type=Path)
    approval_parser.add_argument("--statement", required=True)
    approval_parser.add_argument("--date")
    approval_parser.add_argument("--replace", action="store_true")

    confirm_parser = subparsers.add_parser("confirm", help="Confirm a valid draft pair.")
    confirm_parser.add_argument("--directory", required=True, type=Path)
    confirm_parser.add_argument("--date")

    validate_parser = subparsers.add_parser("validate", help="Validate an artifact pair.")
    validate_parser.add_argument("--directory", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            create_artifacts(
                args.input,
                args.output_dir,
                args.source_review,
                args.interview_state,
            )
            print(f"Created draft artifacts in {args.output_dir}")
        elif args.command == "sync":
            sync_artifacts(
                args.input,
                args.directory,
                args.source_review,
                args.interview_state,
            )
            print(f"Synchronized draft artifacts in {args.directory}")
        elif args.command == "approve":
            record_approval(
                args.directory,
                args.statement,
                args.date,
                args.replace,
            )
            print(f"Recorded explicit approval in {args.directory / APPROVAL_NAME}")
        elif args.command == "confirm":
            confirm_artifacts(args.directory, args.date)
            print(f"Confirmed artifacts in {args.directory}")
        elif args.command == "validate":
            errors = validate_directory(args.directory)
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(f"VALID: {args.directory}")
    except ContextError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
