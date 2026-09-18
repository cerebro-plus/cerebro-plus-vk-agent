from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from common import InputValidationError


def _matches_type(value: Any, expected: str) -> bool:
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


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise InputValidationError(f"Unsupported external schema reference: {ref}")
    current: Any = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            raise InputValidationError(f"Broken schema reference: {ref}")
        current = current[key]
    if not isinstance(current, dict):
        raise InputValidationError(f"Schema reference is not an object: {ref}")
    return current


def _validate_format(value: str, format_name: str, path: str) -> None:
    try:
        if format_name == "date-time":
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone is missing")
        elif format_name == "date":
            dt.date.fromisoformat(value)
        elif format_name == "uri":
            parsed = urlparse(value)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("absolute URI is required")
    except ValueError as exc:
        raise InputValidationError(
            f"{path}: invalid {format_name}: {value!r}"
        ) from exc


def _validate(
    value: Any,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
) -> None:
    if "$ref" in schema:
        _validate(value, _resolve_ref(root, str(schema["$ref"])), root, path)
        return

    expected_type = schema.get("type")
    if expected_type is not None:
        variants = [expected_type] if isinstance(expected_type, str) else list(expected_type)
        if not any(_matches_type(value, item) for item in variants):
            raise InputValidationError(
                f"{path}: expected type {variants}, got {type(value).__name__}"
            )

    if "const" in schema and value != schema["const"]:
        raise InputValidationError(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise InputValidationError(f"{path}: value is outside enum")

    if isinstance(value, str):
        if len(value) < int(schema.get("minLength") or 0):
            raise InputValidationError(f"{path}: string is too short")
        if schema.get("pattern") and not re.search(str(schema["pattern"]), value):
            raise InputValidationError(f"{path}: string does not match pattern")
        if schema.get("format"):
            _validate_format(value, str(schema["format"]), path)

    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and "minimum" in schema
        and value < schema["minimum"]
    ):
        raise InputValidationError(f"{path}: value is below minimum")

    if isinstance(value, list):
        if len(value) < int(schema.get("minItems") or 0):
            raise InputValidationError(f"{path}: array has too few items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, root, f"{path}[{index}]")

    if not isinstance(value, dict):
        return

    required = schema.get("required") or []
    missing = [str(key) for key in required if key not in value]
    if missing:
        raise InputValidationError(f"{path}: missing required keys: {', '.join(missing)}")

    properties = schema.get("properties") or {}
    pattern_properties = schema.get("patternProperties") or {}
    matched_keys: set[str] = set()
    for key, child_schema in properties.items():
        if key in value:
            _validate(value[key], child_schema, root, f"{path}.{key}")
            matched_keys.add(key)
    for key, child_value in value.items():
        for pattern, child_schema in pattern_properties.items():
            if re.search(pattern, str(key)):
                _validate(child_value, child_schema, root, f"{path}.{key}")
                matched_keys.add(key)
    if schema.get("additionalProperties") is False:
        extras = sorted(str(key) for key in value if key not in matched_keys)
        if extras:
            raise InputValidationError(
                f"{path}: unexpected keys: {', '.join(extras)}"
            )


def validate_schema(value: Any, schema_path: Path, label: str | None = None) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputValidationError(
            f"Invalid schema file {schema_path.name}: {exc}"
        ) from exc
    _validate(value, schema, schema, label or schema_path.stem)
