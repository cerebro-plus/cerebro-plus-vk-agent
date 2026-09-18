from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from common import VkApiError
from metrics import deduplicate_posts, filter_fixed_window


API_BASE = "https://api.vk.com/method/"
API_VERSION = "5.199"
FORBIDDEN_METHODS = {"groups.search"}


class VkPreflightError(VkApiError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class VkClient:
    def __init__(
        self,
        token: str,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
    ) -> None:
        self.token = token
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.max_attempts = max_attempts
        self.calls = 0
        self.retries = 0
        self.errors: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any]) -> Any:
        if method in FORBIDDEN_METHODS:
            raise VkApiError(f"Forbidden method for this skill: {method}")
        safe_params = {key: value for key, value in params.items() if key != "access_token"}
        payload = dict(params)
        payload["access_token"] = self.token
        payload["v"] = API_VERSION
        data = urllib.parse.urlencode(payload).encode("utf-8")
        last_error: dict[str, Any] | None = None
        for attempt in range(1, self.max_attempts + 1):
            request = urllib.request.Request(
                API_BASE + method,
                data=data,
                headers={"User-Agent": "analyze-vk-best-posts/1.0"},
                method="POST",
            )
            try:
                with self.opener(request, timeout=45) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.calls += 1
                if "error" not in body:
                    self.sleeper(0.36)
                    return body.get("response")
                error = body["error"]
                code = int(error.get("error_code") or 0)
                last_error = {
                    "method": method,
                    "params_without_token": safe_params,
                    "error_code": code,
                    "error_msg": error.get("error_msg"),
                }
                if code in {1, 6, 9, 10, 29} and attempt < self.max_attempts:
                    self.retries += 1
                    self.sleeper(float(attempt))
                    continue
                break
            except Exception as exc:
                tls_error = isinstance(exc, ssl.SSLError) or (
                    isinstance(exc, urllib.error.URLError)
                    and isinstance(getattr(exc, "reason", None), ssl.SSLError)
                )
                last_error = {
                    "method": method,
                    "params_without_token": safe_params,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "tls_error": tls_error,
                }
                if attempt < self.max_attempts:
                    self.retries += 1
                    self.sleeper(float(attempt))
                    continue
                break
        assert last_error is not None
        self.errors.append(last_error)
        raise VkApiError(
            f"VK API failed after {self.max_attempts} attempts: "
            f"{method}; tls_error={bool(last_error.get('tls_error'))}"
        )

    def preflight(self, probes: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        original_attempts = self.max_attempts
        self.max_attempts = 1
        checked: list[str] = []
        try:
            for method, params in probes:
                self.call(method, params)
                checked.append(method)
        except VkApiError as exc:
            record = self.errors[-1] if self.errors else {}
            code = record.get("error_code")
            message = str(record.get("message") or record.get("error_msg") or exc)
            lowered = message.casefold()
            if code in {5}:
                reason = "invalid_token"
            elif code in {7, 15, 27, 28}:
                reason = "insufficient_permissions"
            elif code:
                reason = "api_rejected"
            elif any(marker in lowered for marker in ("10013", "errno 13", "permission", "blocked", "denied")):
                reason = "network_blocked"
            else:
                reason = "network_unreachable"
            raise VkPreflightError(
                f"VK preflight failed: {reason}; method={record.get('method') or 'unknown'}; code={code}",
                reason_code=reason,
            ) from exc
        finally:
            self.max_attempts = original_attempts
        return {"status": "passed", "methods": checked, "api_calls": len(checked)}

    def wall_window(
        self, community_id: int, start_ts: int, end_ts: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        offset = 0
        pages = 0
        all_items: list[dict[str, Any]] = []
        stop_reason = "wall_exhausted"
        while True:
            response = self.call(
                "wall.get",
                {
                    "owner_id": -int(community_id),
                    "filter": "owner",
                    "count": 100,
                    "offset": offset,
                },
            )
            pages += 1
            items = response.get("items") or []
            if not items:
                break
            all_items.extend(items)
            offset += len(items)
            if offset >= int(response.get("count") or 0):
                break
            unpinned_dates = [
                int(item.get("date") or 0) for item in items if not item.get("is_pinned")
            ]
            if unpinned_dates and max(unpinned_dates) < start_ts:
                stop_reason = "all_unpinned_items_older_than_window"
                break
        unique, duplicates = deduplicate_posts(all_items)
        selected, window_audit = filter_fixed_window(unique, start_ts, end_ts)
        selected.sort(key=lambda item: (int(item.get("date") or 0), int(item.get("id") or 0)))
        return selected, {
            "pages": pages,
            "offset_reached": offset,
            "unique_items_seen": len(unique),
            "selected_posts": len(selected),
            "duplicates_removed": duplicates,
            "stop_reason": stop_reason,
            **window_audit,
        }

    def all_comments(
        self, owner_id: int, post_id: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        offset = 0
        total = None
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        reply_pages = 0
        replies_expected = 0
        replies_collected = 0
        while total is None or offset < total:
            response = self.call(
                "wall.getComments",
                {
                    "owner_id": owner_id,
                    "post_id": post_id,
                    "offset": offset,
                    "count": 100,
                    "sort": "asc",
                    "need_likes": 1,
                    "thread_items_count": 10,
                },
            )
            pages += 1
            total = int(response.get("count") or 0)
            batch = response.get("items") or []
            if not batch:
                break
            for comment in batch:
                key = str(comment.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                thread = comment.get("thread") or {}
                thread_total = int(thread.get("count") or 0)
                replies = list(thread.get("items") or [])
                reply_seen = {str(item.get("id")) for item in replies}
                reply_offset = len(replies)
                while reply_offset < thread_total:
                    thread_response = self.call(
                        "wall.getComments",
                        {
                            "owner_id": owner_id,
                            "post_id": post_id,
                            "comment_id": int(comment["id"]),
                            "offset": reply_offset,
                            "count": 100,
                            "sort": "asc",
                            "need_likes": 1,
                            "thread_items_count": 0,
                        },
                    )
                    reply_pages += 1
                    reply_batch = thread_response.get("items") or []
                    if not reply_batch:
                        break
                    added = 0
                    for reply in reply_batch:
                        reply_key = str(reply.get("id"))
                        if reply_key not in reply_seen:
                            reply_seen.add(reply_key)
                            replies.append(reply)
                            added += 1
                    if not added:
                        break
                    reply_offset += len(reply_batch)
                replies_expected += thread_total
                replies_collected += len(replies)
                item = dict(comment)
                item["_collected_replies"] = replies
                items.append(item)
            offset += len(batch)
        return items, {
            "top_level_expected": total,
            "top_level_collected": len(items),
            "top_level_pages": pages,
            "replies_expected": replies_expected,
            "replies_collected": replies_collected,
            "reply_pages": reply_pages,
        }
