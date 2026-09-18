#!/usr/bin/env python3
"""Track a one-question-at-a-time business and competitor interview."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


BUSINESS_LIMIT = 15
PHASES = {"business", "competitors", "complete"}
QUALITIES = {"complete", "general", "incomplete", "contradictory"}
COMPETITOR_TOPICS = {
    "known_competitors",
    "classification",
    "key_features",
    "feature_matching_policy",
    "search_platforms",
    "target_count",
    "search_geography",
    "vk_audience_size",
    "publication_freshness",
    "required_inclusions",
    "exclusions",
}
SECRET_PATTERNS = (
    re.compile(r"vk1\.[A-Za-z0-9._-]{20,}"),
    re.compile(
        r"(?i)(access[_-]?token|api[_-]?key|client[_-]?secret|password)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
)


class InterviewError(RuntimeError):
    """Raised when the interview protocol would be violated."""


def today_iso() -> str:
    return dt.date.today().isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InterviewError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InterviewError(
            f"Invalid JSON in {path.name}: line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc


def atomic_write_json(path: Path, value: Any) -> None:
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
            handle.write(canonical_json(value))
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise InterviewError(f"Source review not found: {path}") from exc
    return digest.hexdigest()


def normalized_question(value: str) -> str:
    return " ".join(re.findall(r"[0-9a-zа-яё]+", value.lower()))


def secret_findings(value: Any) -> list[str]:
    text = canonical_json(value)
    return [
        "interview_state.json: potential secret detected"
        for pattern in SECRET_PATTERNS
        if pattern.search(text)
    ]


def load_string_list(path: Path | None) -> list[str]:
    if path is None:
        return []
    value = load_json(path)
    if isinstance(value, dict):
        value = value.get("items", value.get("topics", value.get("gaps")))
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise InterviewError(f"{path.name} must contain an array of non-empty strings.")
    return list(dict.fromkeys(item.strip() for item in value))


def create_state(
    source_review: Path,
    resolved_topics: list[str] | None = None,
) -> dict[str, Any]:
    source = load_json(source_review)
    if not isinstance(source, dict) or source.get("status") not in {
        "collected",
        "partial",
    }:
        raise InterviewError(
            "A collected or partial source_review.json is required before interview."
        )
    state = {
        "schema_version": "1.0",
        "status": "active",
        "phase": "business",
        "created_at": today_iso(),
        "updated_at": today_iso(),
        "source_review_sha256": sha256_file(source_review),
        "business_question_limit": BUSINESS_LIMIT,
        "business_questions_asked": 0,
        "competitor_questions_asked": 0,
        "clarification_questions_asked": 0,
        "awaiting_answer": None,
        "resolved_topics": sorted(set(resolved_topics or [])),
        "questions": [],
        "business": {
            "closed": False,
            "remaining_gaps": [],
            "additional_round_offered": False,
            "additional_round_requested": None,
        },
        "competitors": {
            "closed": False,
        },
        "security": {"contains_secrets": False},
    }
    errors = validate_state(state)
    if errors:
        raise InterviewError("\n".join(errors))
    return state


def question_by_number(state: dict[str, Any], number: int) -> dict[str, Any]:
    for item in state["questions"]:
        if item["number"] == number:
            return item
    raise InterviewError(f"Question {number} does not exist.")


def ask_question(
    state: dict[str, Any],
    phase: str,
    topic: str,
    question: str,
    clarification_of: int | None = None,
) -> dict[str, Any]:
    errors = validate_state(state)
    if errors:
        raise InterviewError("Cannot update invalid state:\n" + "\n".join(errors))
    if state["status"] != "active" or state["phase"] == "complete":
        raise InterviewError("Interview is already complete.")
    if phase != state["phase"]:
        raise InterviewError(f"Current phase is {state['phase']}, not {phase}.")
    if state["awaiting_answer"] is not None:
        raise InterviewError(
            f"Question {state['awaiting_answer']} must be answered first."
        )
    cleaned_topic = topic.strip()
    cleaned_question = " ".join(question.split())
    if not cleaned_topic:
        raise InterviewError("Question topic must not be empty.")
    if not cleaned_question or len(cleaned_question) > 300:
        raise InterviewError("Question must contain 1-300 characters.")
    if cleaned_question.count("?") > 1:
        raise InterviewError("Ask one simple question at a time.")
    normalized = normalized_question(cleaned_question)
    if not normalized:
        raise InterviewError("Question has no meaningful text.")
    if normalized in {
        item["normalized_question"] for item in state["questions"]
    }:
        raise InterviewError("This question has already been asked.")

    unresolved = [
        item
        for item in state["questions"]
        if item["answered"] and item["needs_clarification"]
    ]
    pending = unresolved[-1] if unresolved else None
    if pending is not None:
        if clarification_of != pending["number"]:
            raise InterviewError(
                f"Question {pending['number']} needs clarification before a new topic."
            )
    elif clarification_of is not None:
        raise InterviewError("Clarification target does not need clarification.")

    if phase == "business":
        if state["business"]["closed"]:
            raise InterviewError("Business interview block is closed.")
        if state["business_questions_asked"] >= state["business_question_limit"]:
            raise InterviewError("Business interview question limit reached.")
    elif state["competitors"]["closed"]:
        raise InterviewError("Competitor interview block is closed.")

    number = len(state["questions"]) + 1
    item = {
        "number": number,
        "phase": phase,
        "topic": cleaned_topic,
        "question": cleaned_question,
        "normalized_question": normalized,
        "clarification_of": clarification_of,
        "answered": False,
        "answer_summary": None,
        "quality": None,
        "needs_clarification": False,
    }
    if pending is not None:
        pending["needs_clarification"] = False
    state["questions"].append(item)
    state["awaiting_answer"] = number
    if phase == "business":
        state["business_questions_asked"] += 1
    else:
        state["competitor_questions_asked"] += 1
    if clarification_of is not None:
        state["clarification_questions_asked"] += 1
    state["updated_at"] = today_iso()
    return state


def answer_question(
    state: dict[str, Any],
    number: int,
    answer_summary: str,
    quality: str,
) -> dict[str, Any]:
    errors = validate_state(state)
    if errors:
        raise InterviewError("Cannot update invalid state:\n" + "\n".join(errors))
    if quality not in QUALITIES:
        raise InterviewError(f"Unsupported answer quality: {quality}")
    if state["awaiting_answer"] != number:
        raise InterviewError(f"Question {number} is not awaiting an answer.")
    cleaned = " ".join(answer_summary.split())
    if not cleaned:
        raise InterviewError("Answer summary must not be empty.")
    item = question_by_number(state, number)
    item["answered"] = True
    item["answer_summary"] = cleaned
    item["quality"] = quality
    item["needs_clarification"] = quality != "complete"
    state["awaiting_answer"] = None
    if quality == "complete":
        state["resolved_topics"] = sorted(
            set(state["resolved_topics"]) | {item["topic"]}
        )
    state["updated_at"] = today_iso()
    return state


def mark_resolved(state: dict[str, Any], topic: str) -> dict[str, Any]:
    if state["awaiting_answer"] is not None:
        raise InterviewError("Answer the current question before marking another topic.")
    cleaned = topic.strip()
    if not cleaned:
        raise InterviewError("Resolved topic must not be empty.")
    state["resolved_topics"] = sorted(set(state["resolved_topics"]) | {cleaned})
    state["updated_at"] = today_iso()
    return state


def close_business(
    state: dict[str, Any],
    remaining_gaps: list[str],
    additional_round_offered: bool,
    additional_round_requested: str = "pending",
) -> dict[str, Any]:
    errors = validate_state(state)
    if errors:
        raise InterviewError("Cannot update invalid state:\n" + "\n".join(errors))
    if state["phase"] != "business" or state["business"]["closed"]:
        raise InterviewError("Business interview block is not open.")
    if state["awaiting_answer"] is not None:
        raise InterviewError("Answer the current question before closing the block.")
    unresolved = [
        item["number"]
        for item in state["questions"]
        if item["answered"] and item["needs_clarification"]
    ]
    if unresolved:
        raise InterviewError(
            "Resolve pending clarification before closing: "
            + ", ".join(map(str, unresolved))
        )
    if not additional_round_offered:
        raise InterviewError("An additional interview round must be explicitly offered.")
    requested_map = {"yes": True, "no": False, "pending": None}
    if additional_round_requested not in requested_map:
        raise InterviewError("Invalid additional-round response.")
    state["business"] = {
        "closed": True,
        "remaining_gaps": list(dict.fromkeys(remaining_gaps)),
        "additional_round_offered": True,
        "additional_round_requested": requested_map[additional_round_requested],
    }
    state["phase"] = "competitors"
    state["updated_at"] = today_iso()
    return state


def close_competitors(state: dict[str, Any]) -> dict[str, Any]:
    errors = validate_state(state)
    if errors:
        raise InterviewError("Cannot update invalid state:\n" + "\n".join(errors))
    if state["phase"] != "competitors" or state["competitors"]["closed"]:
        raise InterviewError("Competitor interview block is not open.")
    if state["awaiting_answer"] is not None:
        raise InterviewError("Answer the current question before closing the block.")
    missing = sorted(COMPETITOR_TOPICS - set(state["resolved_topics"]))
    if missing:
        raise InterviewError(
            "Competitor block has unresolved required topics: " + ", ".join(missing)
        )
    state["competitors"]["closed"] = True
    state["phase"] = "complete"
    state["status"] = "complete"
    state["updated_at"] = today_iso()
    return state


def validate_state(state: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(state, dict):
        return ["interview_state.json: root must be an object"]
    if state.get("schema_version") != "1.0":
        errors.append("$.schema_version: expected 1.0")
    if state.get("status") not in {"active", "complete"}:
        errors.append("$.status: invalid status")
    if state.get("phase") not in PHASES:
        errors.append("$.phase: invalid phase")
    if state.get("business_question_limit") != BUSINESS_LIMIT:
        errors.append(f"$.business_question_limit: expected {BUSINESS_LIMIT}")
    questions = state.get("questions")
    if not isinstance(questions, list):
        errors.append("$.questions: expected array")
        questions = []
    numbers: list[int] = []
    normalized: list[str] = []
    business_count = 0
    competitor_count = 0
    clarification_count = 0
    unanswered: list[int] = []
    for index, item in enumerate(questions):
        path = f"$.questions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path}: expected object")
            continue
        number = item.get("number")
        if not isinstance(number, int) or number <= 0:
            errors.append(f"{path}.number: expected positive integer")
        else:
            numbers.append(number)
        phase = item.get("phase")
        if phase == "business":
            business_count += 1
        elif phase == "competitors":
            competitor_count += 1
        else:
            errors.append(f"{path}.phase: invalid question phase")
        question = str(item.get("question") or "")
        expected_normalized = normalized_question(question)
        if item.get("normalized_question") != expected_normalized:
            errors.append(f"{path}.normalized_question: inconsistent")
        normalized.append(expected_normalized)
        clarification_of = item.get("clarification_of")
        if clarification_of is not None:
            clarification_count += 1
            if not isinstance(clarification_of, int) or clarification_of >= number:
                errors.append(f"{path}.clarification_of: invalid reference")
        answered = item.get("answered")
        if not isinstance(answered, bool):
            errors.append(f"{path}.answered: expected boolean")
        elif not answered and isinstance(number, int):
            unanswered.append(number)
        if answered and item.get("quality") not in QUALITIES:
            errors.append(f"{path}.quality: invalid answered quality")
        if not answered and (
            item.get("quality") is not None or item.get("answer_summary") is not None
        ):
            errors.append(f"{path}: unanswered question has answer data")
    if numbers != list(range(1, len(numbers) + 1)):
        errors.append("$.questions: numbers must be consecutive")
    meaningful = [item for item in normalized if item]
    if len(meaningful) != len(set(meaningful)):
        errors.append("$.questions: duplicate question detected")
    if business_count != state.get("business_questions_asked"):
        errors.append("$.business_questions_asked: inconsistent count")
    if business_count > BUSINESS_LIMIT:
        errors.append("$.business_questions_asked: limit exceeded")
    if competitor_count != state.get("competitor_questions_asked"):
        errors.append("$.competitor_questions_asked: inconsistent count")
    if clarification_count != state.get("clarification_questions_asked"):
        errors.append("$.clarification_questions_asked: inconsistent count")
    awaiting = state.get("awaiting_answer")
    if len(unanswered) > 1:
        errors.append("$.questions: only one unanswered question is allowed")
    expected_awaiting = unanswered[0] if unanswered else None
    if awaiting != expected_awaiting:
        errors.append("$.awaiting_answer: inconsistent")
    resolved = state.get("resolved_topics")
    if not isinstance(resolved, list) or any(
        not isinstance(item, str) or not item for item in resolved
    ):
        errors.append("$.resolved_topics: expected string array")
    business = state.get("business")
    competitors = state.get("competitors")
    if not isinstance(business, dict):
        errors.append("$.business: expected object")
    if not isinstance(competitors, dict):
        errors.append("$.competitors: expected object")
    if state.get("phase") in {"competitors", "complete"} and isinstance(
        business, dict
    ):
        if business.get("closed") is not True:
            errors.append("$.business.closed: must be true after business phase")
        if business.get("additional_round_offered") is not True:
            errors.append("$.business.additional_round_offered: must be true")
    if state.get("phase") == "complete":
        if state.get("status") != "complete":
            errors.append("$.status: complete phase requires complete status")
        if not isinstance(competitors, dict) or competitors.get("closed") is not True:
            errors.append("$.competitors.closed: must be true")
    if state.get("security") != {"contains_secrets": False}:
        errors.append("$.security: unsafe declaration")
    source_hash = state.get("source_review_sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(
        r"[a-f0-9]{64}", source_hash
    ):
        errors.append("$.source_review_sha256: invalid SHA-256")
    errors.extend(secret_findings(state))
    return sorted(set(errors))


def update_file(path: Path, state: dict[str, Any]) -> None:
    errors = validate_state(state)
    if errors:
        raise InterviewError("Refusing to write invalid state:\n" + "\n".join(errors))
    atomic_write_json(path, state)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--source-review", required=True, type=Path)
    init.add_argument("--resolved-topics-file", type=Path)
    init.add_argument("--output", required=True, type=Path)

    ask = sub.add_parser("ask")
    ask.add_argument("--state", required=True, type=Path)
    ask.add_argument("--phase", required=True, choices=("business", "competitors"))
    ask.add_argument("--topic", required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--clarification-of", type=int)

    answer = sub.add_parser("answer")
    answer.add_argument("--state", required=True, type=Path)
    answer.add_argument("--number", required=True, type=int)
    answer.add_argument("--quality", required=True, choices=sorted(QUALITIES))
    answer.add_argument("--answer", required=True)

    resolved = sub.add_parser("mark-resolved")
    resolved.add_argument("--state", required=True, type=Path)
    resolved.add_argument("--topic", required=True)

    close_business_parser = sub.add_parser("close-business")
    close_business_parser.add_argument("--state", required=True, type=Path)
    close_business_parser.add_argument("--gaps-file", type=Path)
    close_business_parser.add_argument(
        "--additional-round-offered",
        action="store_true",
    )
    close_business_parser.add_argument(
        "--additional-round-requested",
        choices=("yes", "no", "pending"),
        default="pending",
    )

    close_competitors_parser = sub.add_parser("close-competitors")
    close_competitors_parser.add_argument("--state", required=True, type=Path)

    validate = sub.add_parser("validate")
    validate.add_argument("--state", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            if args.output.exists():
                raise InterviewError("Output already exists; use a new run directory.")
            state = create_state(
                args.source_review,
                load_string_list(args.resolved_topics_file),
            )
            atomic_write_json(args.output, state)
            print(f"Initialized interview state: {args.output}")
            return 0

        state = load_json(args.state)
        if args.command == "ask":
            state = ask_question(
                state,
                args.phase,
                args.topic,
                args.question,
                args.clarification_of,
            )
            update_file(args.state, state)
            print(f"Question {state['awaiting_answer']} recorded.")
        elif args.command == "answer":
            state = answer_question(
                state,
                args.number,
                args.answer,
                args.quality,
            )
            update_file(args.state, state)
            print(f"Answer {args.number} recorded as {args.quality}.")
        elif args.command == "mark-resolved":
            state = mark_resolved(state, args.topic)
            update_file(args.state, state)
            print(f"Resolved topic recorded: {args.topic}")
        elif args.command == "close-business":
            state = close_business(
                state,
                load_string_list(args.gaps_file),
                args.additional_round_offered,
                args.additional_round_requested,
            )
            update_file(args.state, state)
            print("Business block closed; competitor block opened.")
        elif args.command == "close-competitors":
            state = close_competitors(state)
            update_file(args.state, state)
            print("Competitor block closed; interview complete.")
        else:
            errors = validate_state(state)
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(f"VALID: {args.state}")
    except (InterviewError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
