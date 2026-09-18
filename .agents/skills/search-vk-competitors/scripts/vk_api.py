from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


TRANSIENT_VK_ERRORS = {1, 6, 9, 10, 29}


class VKAPIError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class VKPreflightError(VKAPIError):
    def __init__(self, message: str, *, reason_code: str, code: int | None = None) -> None:
        super().__init__(message, code=code)
        self.reason_code = reason_code


@dataclass
class RetryPolicy:
    attempts: int = 5
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_seconds: float = 0.2


class VKClient:
    """Small VK API client that never logs or serializes the access token."""

    def __init__(
        self,
        token: str,
        *,
        api_version: str = "5.199",
        retry_policy: RetryPolicy | None = None,
        timeout_seconds: float = 30.0,
        min_interval_seconds: float = 0.34,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token or token.isspace():
            raise ValueError("VK token is empty")
        self._token = token.strip()
        self.api_version = api_version
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._sleeper = sleeper
        self._opener = opener
        self._last_call_started = 0.0
        self.calls = 0
        self.errors: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = dict(params or {})
        payload["access_token"] = self._token
        payload["v"] = self.api_version
        url = f"https://api.vk.com/method/{method}"
        encoded = urllib.parse.urlencode(payload, doseq=True).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(1, self.retry_policy.attempts + 1):
            elapsed = time.monotonic() - self._last_call_started
            if elapsed < self.min_interval_seconds:
                self._sleeper(self.min_interval_seconds - elapsed)
            self._last_call_started = time.monotonic()
            self.calls += 1
            try:
                request = urllib.request.Request(
                    url,
                    data=encoded,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    method="POST",
                )
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    document = json.loads(response.read().decode("utf-8"))
                if "error" in document:
                    error = document["error"]
                    code = int(error.get("error_code", 0)) or None
                    message = str(error.get("error_msg", "VK API error"))
                    exc = VKAPIError(f"{method}: {message}", code=code)
                    if code not in TRANSIENT_VK_ERRORS:
                        self.errors.append(
                            {"method": method, "code": code, "message": message}
                        )
                        raise exc
                    last_error = exc
                else:
                    return document.get("response")
            except VKAPIError:
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc

            if attempt < self.retry_policy.attempts:
                delay = min(
                    self.retry_policy.max_delay_seconds,
                    self.retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
                )
                delay += random.uniform(0, self.retry_policy.jitter_seconds)
                self._sleeper(delay)

        message = str(last_error) if last_error else "unknown error"
        self.errors.append({"method": method, "code": None, "message": message, "error_type": type(last_error).__name__ if last_error else "unknown"})
        raise VKAPIError(
            f"{method}: request failed after {self.retry_policy.attempts} attempts"
        )

    def preflight(self, probes: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        original_policy = self.retry_policy
        self.retry_policy = RetryPolicy(
            attempts=1,
            base_delay_seconds=0,
            max_delay_seconds=0,
            jitter_seconds=0,
        )
        checked: list[str] = []
        try:
            for method, params in probes:
                self.call(method, params)
                checked.append(method)
        except VKAPIError as exc:
            record = self.errors[-1] if self.errors else {}
            code = exc.code or record.get("code")
            message = str(record.get("message") or exc)
            lowered = message.casefold()
            if code in {5}:
                reason = "invalid_token"
            elif code in {7, 15, 27, 28}:
                reason = "insufficient_permissions"
            elif code is not None:
                reason = "api_rejected"
            elif any(marker in lowered for marker in ("10013", "errno 13", "permission", "blocked", "denied")):
                reason = "network_blocked"
            else:
                reason = "network_unreachable"
            raise VKPreflightError(
                f"VK preflight failed: {reason}; method={record.get('method') or 'unknown'}; code={code}",
                reason_code=reason,
                code=code,
            ) from exc
        finally:
            self.retry_policy = original_policy
        return {"status": "passed", "methods": checked, "api_calls": len(checked)}


def response_items(response: Any) -> tuple[int, list[dict[str, Any]]]:
    if isinstance(response, dict):
        items = response.get("items")
        if items is None and isinstance(response.get("groups"), list):
            items = response["groups"]
        if items is None:
            items = []
        return int(response.get("count", len(items))), list(items)
    if isinstance(response, list):
        return len(response), list(response)
    return 0, []


def dedupe_candidates(
    candidates: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    unique: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_id = int(candidate.get("id") or candidate.get("community_id") or 0)
        if candidate_id <= 0:
            continue
        if candidate_id not in unique:
            unique[candidate_id] = dict(candidate)
        else:
            for key in ("matched_queries", "query_origins"):
                existing = set(unique[candidate_id].get(key, []))
                existing.update(candidate.get(key, []))
                unique[candidate_id][key] = sorted(existing)
            for key in ("known_competitor", "required_inclusion"):
                unique[candidate_id][key] = bool(
                    unique[candidate_id].get(key) or candidate.get(key)
                )
    return [unique[key] for key in sorted(unique)]


def discover_groups(
    call: Callable[[str, dict[str, Any]], Any],
    queries: Iterable[str],
    *,
    page_size: int = 1000,
    max_pages_per_query: int = 100,
    candidate_cap: int | None = None,
) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []
    for query in dict.fromkeys(value.strip() for value in queries if value.strip()):
        offset = 0
        for _ in range(max_pages_per_query):
            total, items = response_items(
                call(
                    "groups.search",
                    {
                        "q": query,
                        "count": page_size,
                        "offset": offset,
                        "sort": 0,
                    },
                )
            )
            for item in items:
                enriched = dict(item)
                enriched["matched_queries"] = sorted(
                    set(enriched.get("matched_queries", [])) | {query}
                )
                discovered.append(enriched)
            if not items or offset + len(items) >= total:
                break
            offset += len(items)
            if candidate_cap and len(dedupe_candidates(discovered)) >= candidate_cap:
                break
        if candidate_cap and len(dedupe_candidates(discovered)) >= candidate_cap:
            break
    result = dedupe_candidates(discovered)
    return result[:candidate_cap] if candidate_cap else result


def resolve_known_groups(
    call: Callable[[str, dict[str, Any]], Any],
    references: Iterable[str],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for reference in references:
        parsed = urlparse(reference)
        path = parsed.path if parsed.scheme and parsed.netloc else reference
        screen_name = path.rstrip("/").split("/")[-1].strip().rstrip(".,;:")
        if not screen_name:
            continue
        response = call(
            "utils.resolveScreenName",
            {"screen_name": screen_name},
        )
        if (
            isinstance(response, dict)
            and response.get("type") == "group"
            and int(response.get("object_id", 0)) > 0
        ):
            resolved.append(
                {
                    "id": int(response["object_id"]),
                    "screen_name": screen_name,
                    "matched_queries": ["known_competitor"],
                }
            )
    return dedupe_candidates(resolved)


def resolve_reference_groups(
    call: Callable[[str, dict[str, Any]], Any],
    references: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for reference in references:
        name = str(reference.get("name") or "").strip()
        url = str(reference.get("url") or "").strip()
        origin = str(reference.get("origin") or "known").strip()
        parsed = urlparse(url)
        path = parsed.path if parsed.scheme and parsed.netloc else url
        screen_name = (
            path.rstrip("/").split("/")[-1].strip().rstrip(".,;:")
            if path
            else ""
        )
        community_ids: list[int] = []
        error: str | None = None
        if screen_name:
            try:
                response = call(
                    "utils.resolveScreenName",
                    {"screen_name": screen_name},
                )
                if (
                    isinstance(response, dict)
                    and response.get("type") == "group"
                    and int(response.get("object_id", 0)) > 0
                ):
                    community_id = int(response["object_id"])
                    community_ids.append(community_id)
                    resolved.append(
                        {
                            "id": community_id,
                            "screen_name": screen_name,
                            "matched_queries": [f"{origin}:{name or screen_name}"],
                            "query_origins": [origin],
                            "known_competitor": origin == "known",
                            "required_inclusion": origin == "required_inclusion",
                        }
                    )
            except VKAPIError as exc:
                error = str(exc)
        audit.append(
            {
                "name": name or None,
                "url": url or None,
                "origin": origin,
                "resolved_community_ids": community_ids,
                "status": "resolved" if community_ids else "not_resolved_by_url",
                "error": error,
            }
        )
    return dedupe_candidates(resolved), audit


def enrich_groups(
    call: Callable[[str, dict[str, Any]], Any],
    candidates: Iterable[dict[str, Any]],
    *,
    batch_size: int = 500,
) -> list[dict[str, Any]]:
    source = dedupe_candidates(candidates)
    by_id = {int(item["id"]): dict(item) for item in source}
    ids = list(by_id)
    fields = (
        "members_count,city,country,site,description,status,activity,"
        "contacts,links,type,is_closed,deactivated"
    )
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        response = call(
            "groups.getById",
            {"group_ids": ",".join(map(str, batch)), "fields": fields},
        )
        _, groups = response_items(response)
        for group in groups:
            group_id = int(group.get("id", 0))
            if group_id not in by_id:
                continue
            queries = by_id[group_id].get("matched_queries", [])
            by_id[group_id].update(group)
            by_id[group_id]["matched_queries"] = queries
    return [by_id[key] for key in sorted(by_id)]


def dedupe_posts(posts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for post in posts:
        owner_id = int(post.get("owner_id", 0))
        post_id = int(post.get("id") or post.get("post_id") or 0)
        if not owner_id or not post_id:
            continue
        unique.setdefault((owner_id, post_id), dict(post))
    return sorted(
        unique.values(),
        key=lambda item: (
            -int(item.get("date", 0)),
            int(item.get("owner_id", 0)),
            int(item.get("id") or item.get("post_id") or 0),
        ),
    )


def collect_wall_posts(
    call: Callable[[str, dict[str, Any]], Any],
    community_id: int,
    *,
    window_start_epoch: int,
    window_end_epoch: int,
    page_size: int = 100,
    max_pages: int = 1000,
) -> dict[str, Any]:
    owner_id = -abs(int(community_id))
    offset = 0
    all_own_posts: list[dict[str, Any]] = []
    pages = 0
    stop_reason = "max_pages"

    for _ in range(max_pages):
        pages += 1
        _, items = response_items(
            call(
                "wall.get",
                {
                    "owner_id": owner_id,
                    "filter": "owner",
                    "count": page_size,
                    "offset": offset,
                },
            )
        )
        own_items = [
            item for item in items if int(item.get("from_id", owner_id)) == owner_id
        ]
        all_own_posts.extend(own_items)

        if not items:
            stop_reason = "wall_end"
            break
        if len(items) < page_size:
            stop_reason = "wall_end"
            break

        non_pinned = [item for item in own_items if not bool(item.get("is_pinned"))]
        if non_pinned and all(
            int(item.get("date", 0)) < window_start_epoch for item in non_pinned
        ):
            stop_reason = "all_non_pinned_older_than_window"
            break
        offset += len(items)

    unique = dedupe_posts(all_own_posts)
    latest = max(
        (int(item.get("date", 0)) for item in unique),
        default=None,
    )
    in_window = [
        item
        for item in unique
        if window_start_epoch
        <= int(item.get("date", 0))
        <= window_end_epoch
    ]
    return {
        "community_id": abs(int(community_id)),
        "owner_id": owner_id,
        "pages_fetched": pages,
        "stop_reason": stop_reason,
        "latest_own_publication_epoch": latest,
        "posts": in_window,
    }


def utc_iso_from_epoch(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
