#!/usr/bin/env python3
"""Safely store a VK token and collect public business-source context."""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


VK_API_VERSION = "5.199"
VK_API_BASE = "https://api.vk.com/method"
DEFAULT_TOKEN_NAME = "vk_api_token.txt"
MAX_WEB_BYTES = 2_000_000
MAX_WEB_TEXT = 40_000
MAX_POSTS = 100
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{20,}$")
SECRET_PATTERNS = (
    re.compile(r"vk1\.[A-Za-z0-9._-]{20,}"),
    re.compile(
        r"(?i)(access[_-]?token|api[_-]?key|client[_-]?secret|password|token)"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{12,}"
    ),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
)
VK_HOSTS = {"vk.com", "www.vk.com", "vk.ru", "www.vk.ru"}
VK_RESERVED_PATHS = {
    "",
    "feed",
    "im",
    "login",
    "market",
    "search",
    "settings",
}


class SourceError(RuntimeError):
    """Raised when source intake cannot safely continue."""


class VKAPIError(SourceError):
    """A redacted VK API error."""


def today_iso() -> str:
    return dt.date.today().isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
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
            handle.write(text)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        if mode is not None:
            os.chmod(path, mode)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, canonical_json(value))


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(
            f"Invalid JSON in {path.name}: line {exc.lineno}, "
            f"column {exc.colno}: {exc.msg}"
        ) from exc


def secret_findings(value: Any, label: str) -> list[str]:
    text = value if isinstance(value, str) else canonical_json(value)
    return [
        f"{label}: potential secret detected"
        for pattern in SECRET_PATTERNS
        if pattern.search(text)
    ]


def normalized_token(token: str) -> str:
    value = token.strip()
    if not TOKEN_RE.fullmatch(value) or any(character.isspace() for character in value):
        raise SourceError("VK token has an invalid format.")
    return value


def resolve_inside(root: Path, relative_or_absolute: Path) -> Path:
    root_resolved = root.resolve()
    candidate = (
        relative_or_absolute.resolve()
        if relative_or_absolute.is_absolute()
        else (root_resolved / relative_or_absolute).resolve()
    )
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise SourceError("Token file must stay inside the project directory.") from exc
    return candidate


def ensure_gitignore_entry(workspace_root: Path, token_path: Path) -> None:
    ignore_path = workspace_root / ".gitignore"
    relative = token_path.relative_to(workspace_root).as_posix()
    existing = ""
    if ignore_path.exists():
        existing = ignore_path.read_text(encoding="utf-8")
    lines = {line.strip() for line in existing.splitlines()}
    if relative in lines or f"/{relative}" in lines:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    atomic_write_text(ignore_path, existing + prefix + f"/{relative}\n")


def save_token(
    workspace_root: Path,
    token: str,
    token_name: str = DEFAULT_TOKEN_NAME,
    force: bool = False,
) -> Path:
    root = workspace_root.resolve()
    if not root.is_dir():
        raise SourceError(f"Project directory does not exist: {root}")
    token_path = resolve_inside(root, Path(token_name))
    if token_path.suffix.lower() != ".txt":
        raise SourceError("VK token must be stored in a .txt file.")
    if token_path.exists() and not force:
        raise SourceError(
            "VK token file already exists; explicit --force is required to replace it."
        )
    atomic_write_text(token_path, normalized_token(token) + "\n", mode=0o600)
    ensure_gitignore_entry(root, token_path)
    return token_path


def read_token(token_path: Path) -> str:
    try:
        return normalized_token(token_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SourceError(f"VK token file not found: {token_path}") from exc


def request_json(url: str, body: bytes, timeout: float) -> dict[str, Any]:
    request = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "business-context-interview/1.1",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        raise VKAPIError(f"VK API HTTP error: {exc.code}") from exc
    except URLError as exc:
        raise VKAPIError("VK API network error.") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VKAPIError("VK API returned invalid JSON.") from exc
    if not isinstance(value, dict):
        raise VKAPIError("VK API returned an unexpected response.")
    return value


class VKClient:
    def __init__(
        self,
        token: str,
        version: str = VK_API_VERSION,
        timeout: float = 30.0,
        requester: Callable[[str, bytes, float], dict[str, Any]] | None = None,
    ) -> None:
        self._token = normalized_token(token)
        self.version = version
        self.timeout = timeout
        self.requester = requester or request_json

    def call(self, method: str, parameters: dict[str, Any]) -> Any:
        payload = {
            key: value
            for key, value in parameters.items()
            if value is not None
        }
        payload["access_token"] = self._token
        payload["v"] = self.version
        encoded = urlencode(payload, doseq=True).encode("utf-8")
        value = self.requester(
            f"{VK_API_BASE}/{method}",
            encoded,
            self.timeout,
        )
        error = value.get("error")
        if isinstance(error, dict):
            code = error.get("error_code", "unknown")
            message = str(error.get("error_msg") or "VK API request failed")
            raise VKAPIError(f"VK API error {code}: {message}")
        if "response" not in value:
            raise VKAPIError("VK API response has no response field.")
        return value["response"]


def response_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("groups", "items"):
            items = value.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
    return []


def source_kind(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return "vk" if host in VK_HOSTS else "website"


def validate_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SourceError(f"Expected an absolute HTTP(S) URL: {url}")
    return url


def vk_slug(url: str) -> str:
    parsed = urlparse(validate_http_url(url))
    if (parsed.hostname or "").lower() not in VK_HOSTS:
        raise SourceError("URL is not a VK community URL.")
    slug = parsed.path.strip("/").split("/", 1)[0]
    if slug.lower() in VK_RESERVED_PATHS:
        raise SourceError("VK community URL has no community screen name.")
    return slug


def clean_count(value: Any) -> int:
    if isinstance(value, dict):
        value = value.get("count")
    return value if isinstance(value, int) and value >= 0 else 0


def project_group(group: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "name",
        "screen_name",
        "description",
        "activity",
        "status",
        "site",
        "members_count",
        "type",
        "is_closed",
    )
    result = {key: group.get(key) for key in keys if group.get(key) is not None}
    for key in ("city", "country"):
        value = group.get(key)
        if isinstance(value, dict):
            result[key] = {
                item: value.get(item)
                for item in ("id", "title")
                if value.get(item) is not None
            }
    for key in ("contacts", "links"):
        values = group.get(key)
        if isinstance(values, list):
            result[key] = values
    return result


def project_post(post: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": post.get("id"),
        "date": post.get("date"),
        "text": str(post.get("text") or "")[:12_000],
        "comments": clean_count(post.get("comments")),
        "likes": clean_count(post.get("likes")),
        "reposts": clean_count(post.get("reposts")),
        "views": clean_count(post.get("views")),
    }
    attachments = post.get("attachments")
    if isinstance(attachments, list):
        result["attachment_types"] = sorted(
            {
                str(item.get("type"))
                for item in attachments
                if isinstance(item, dict) and item.get("type")
            }
        )
    return result


def project_market_item(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": item.get("id"),
        "title": item.get("title"),
        "description": str(item.get("description") or "")[:8_000],
        "availability": item.get("availability"),
        "date": item.get("date"),
    }
    price = item.get("price")
    if isinstance(price, dict):
        result["price"] = {
            key: price.get(key)
            for key in ("amount", "text")
            if price.get(key) is not None
        }
        currency = price.get("currency")
        if isinstance(currency, dict):
            result["price"]["currency"] = {
                key: currency.get(key)
                for key in ("id", "name")
                if currency.get(key) is not None
            }
    category = item.get("category")
    if isinstance(category, dict):
        result["category"] = {
            key: category.get(key)
            for key in ("id", "name", "section")
            if category.get(key) is not None
        }
    return result


def project_address(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key)
        for key in (
            "id",
            "title",
            "address",
            "city_id",
            "latitude",
            "longitude",
            "work_info_status",
        )
        if item.get(key) is not None
    }


def collect_vk_source(
    entry: dict[str, str],
    client: VKClient,
    posts_limit: int = MAX_POSTS,
) -> dict[str, Any]:
    url = entry["url"]
    slug = vk_slug(url)
    fields = ",".join(
        (
            "activity",
            "addresses",
            "city",
            "contacts",
            "country",
            "description",
            "links",
            "members_count",
            "site",
            "status",
        )
    )
    profile_response = client.call(
        "groups.getById",
        {"group_ids": slug, "fields": fields},
    )
    groups = response_items(profile_response)
    if not groups:
        raise VKAPIError("VK community was not returned by groups.getById.")
    group = groups[0]
    group_id = group.get("id")
    if not isinstance(group_id, int) or group_id <= 0:
        raise VKAPIError("VK community has an invalid id.")

    limitations: list[str] = []
    coverage = ["community_profile"]
    content: dict[str, Any] = {"community": project_group(group)}

    wall_response = client.call(
        "wall.get",
        {
            "owner_id": -group_id,
            "count": max(1, min(posts_limit, MAX_POSTS)),
            "filter": "owner",
        },
    )
    content["posts"] = [project_post(item) for item in response_items(wall_response)]
    coverage.append("wall_posts")

    try:
        market_response = client.call(
            "market.get",
            {"owner_id": -group_id, "count": 100, "extended": 0},
        )
        content["market_items"] = [
            project_market_item(item) for item in response_items(market_response)
        ]
        coverage.append("market_items")
    except VKAPIError as exc:
        limitations.append(f"market.get unavailable: {exc}")

    try:
        address_response = client.call(
            "groups.getAddresses",
            {"group_id": group_id, "count": 100},
        )
        content["addresses"] = [
            project_address(item) for item in response_items(address_response)
        ]
        coverage.append("addresses")
    except VKAPIError as exc:
        limitations.append(f"groups.getAddresses unavailable: {exc}")

    return {
        "url": url,
        "final_url": f"https://vk.com/{group.get('screen_name') or slug}",
        "label": entry["label"],
        "kind": "vk",
        "status": "partial" if limitations else "collected",
        "retrieved_at": today_iso(),
        "method": f"VK API {client.version}",
        "coverage": coverage,
        "limitations": limitations,
        "content": content,
    }


class VisibleHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden_depth = 0
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False
        self.meta_description: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            values = {key.lower(): value for key, value in attrs if value is not None}
            name = (values.get("name") or values.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.meta_description:
                self.meta_description = values.get("content")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.hidden_depth:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        self.text_parts.append(cleaned)
        if self.in_title:
            self.title_parts.append(cleaned)


def fetch_web(url: str, timeout: float = 30.0) -> dict[str, Any]:
    request = Request(
        validate_http_url(url),
        headers={"User-Agent": "Mozilla/5.0 business-context-interview/1.1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            raw = response.read(MAX_WEB_BYTES + 1)
            final_url = response.geturl()
    except HTTPError as exc:
        raise SourceError(f"Website HTTP error: {exc.code}") from exc
    except URLError as exc:
        raise SourceError("Website network error.") from exc
    if len(raw) > MAX_WEB_BYTES:
        raw = raw[:MAX_WEB_BYTES]
        truncated = True
    else:
        truncated = False
    charset_match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return {
        "final_url": final_url,
        "content_type": content_type,
        "text": text,
        "truncated": truncated,
    }


def collect_web_source(
    entry: dict[str, str],
    fetcher: Callable[[str, float], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fetch = fetcher or fetch_web
    fetched = fetch(entry["url"], 30.0)
    parser = VisibleHTML()
    parser.feed(str(fetched.get("text") or ""))
    visible = " ".join(parser.text_parts)
    limitations: list[str] = []
    if fetched.get("truncated"):
        limitations.append(f"response truncated at {MAX_WEB_BYTES} bytes")
    if len(visible) > MAX_WEB_TEXT:
        visible = visible[:MAX_WEB_TEXT]
        limitations.append(f"visible text truncated at {MAX_WEB_TEXT} characters")
    if not visible:
        limitations.append("no visible text extracted; JavaScript rendering may be required")
    return {
        "url": entry["url"],
        "final_url": validate_http_url(str(fetched.get("final_url") or entry["url"])),
        "label": entry["label"],
        "kind": "website",
        "status": "partial" if limitations else "collected",
        "retrieved_at": today_iso(),
        "method": "HTTP HTML extraction",
        "coverage": ["title", "meta_description", "visible_text"],
        "limitations": limitations,
        "content": {
            "title": " ".join(parser.title_parts) or None,
            "meta_description": parser.meta_description,
            "visible_text": visible,
        },
    }


def normalize_source_list(value: Any) -> list[dict[str, str]]:
    items = value.get("sources") if isinstance(value, dict) else value
    if not isinstance(items, list) or not items:
        raise SourceError("Source list must contain a non-empty sources array.")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if isinstance(item, str):
            url = validate_http_url(item)
            label = f"Source {index + 1}"
        elif isinstance(item, dict):
            url = validate_http_url(str(item.get("url") or ""))
            label = str(item.get("label") or f"Source {index + 1}").strip()
        else:
            raise SourceError(f"Source {index + 1} must be a URL or object.")
        if url in seen:
            raise SourceError(f"Duplicate source URL: {url}")
        seen.add(url)
        normalized.append({"url": url, "label": label, "kind": source_kind(url)})
    return normalized


def collect_sources(
    entries: list[dict[str, str]],
    token: str | None,
    requester: Callable[[str, bytes, float], dict[str, Any]] | None = None,
    web_fetcher: Callable[[str, float], dict[str, Any]] | None = None,
    posts_limit: int = MAX_POSTS,
) -> dict[str, Any]:
    needs_vk = any(entry["kind"] == "vk" for entry in entries)
    if needs_vk and not token:
        raise SourceError("VK token is required for VK sources.")
    client = VKClient(token, requester=requester) if needs_vk and token else None
    results: list[dict[str, Any]] = []
    for entry in entries:
        try:
            if entry["kind"] == "vk":
                assert client is not None
                result = collect_vk_source(entry, client, posts_limit)
            else:
                result = collect_web_source(entry, web_fetcher)
        except SourceError as exc:
            result = {
                "url": entry["url"],
                "final_url": entry["url"],
                "label": entry["label"],
                "kind": entry["kind"],
                "status": "failed",
                "retrieved_at": today_iso(),
                "method": "VK API" if entry["kind"] == "vk" else "HTTP HTML extraction",
                "coverage": [],
                "limitations": [str(exc)],
                "content": {},
            }
        results.append(result)
    statuses = [item["status"] for item in results]
    overall = (
        "failed"
        if all(status == "failed" for status in statuses)
        else "collected"
        if all(status == "collected" for status in statuses)
        else "partial"
    )
    value = {
        "schema_version": "1.0",
        "status": overall,
        "collected_at": today_iso(),
        "sources": results,
        "security": {
            "contains_secrets": False,
            "token_embedded": False,
            "vk_api_used": needs_vk,
        },
    }
    errors = validate_source_review(value)
    if errors:
        raise SourceError("\n".join(errors))
    return value


def validate_source_review(value: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["source_review.json: root must be an object"]
    if value.get("schema_version") != "1.0":
        errors.append("$.schema_version: expected 1.0")
    if value.get("status") not in {"collected", "partial", "failed"}:
        errors.append("$.status: invalid status")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("$.sources: non-empty array is required")
    else:
        seen: set[str] = set()
        for index, item in enumerate(sources):
            path = f"$.sources[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{path}: expected object")
                continue
            try:
                url = validate_http_url(str(item.get("url") or ""))
            except SourceError as exc:
                errors.append(f"{path}.url: {exc}")
                url = ""
            if url in seen:
                errors.append(f"{path}.url: duplicate URL")
            seen.add(url)
            if item.get("kind") not in {"vk", "website"}:
                errors.append(f"{path}.kind: invalid kind")
            if item.get("status") not in {"collected", "partial", "failed"}:
                errors.append(f"{path}.status: invalid status")
            if not isinstance(item.get("coverage"), list):
                errors.append(f"{path}.coverage: expected array")
            if not isinstance(item.get("limitations"), list):
                errors.append(f"{path}.limitations: expected array")
            if not isinstance(item.get("content"), dict):
                errors.append(f"{path}.content: expected object")
    security = value.get("security")
    expected_security = {
        "contains_secrets": False,
        "token_embedded": False,
        "vk_api_used": any(
            isinstance(item, dict) and item.get("kind") == "vk"
            for item in (sources or [])
        ),
    }
    if security != expected_security:
        errors.append("$.security: unsafe or inconsistent security declaration")
    errors.extend(secret_findings(value, "source_review.json"))
    return sorted(set(errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    save = commands.add_parser("save-token", help="Store VK token using hidden input.")
    save.add_argument("--workspace-root", required=True, type=Path)
    save.add_argument("--token-name", default=DEFAULT_TOKEN_NAME)
    save.add_argument("--force", action="store_true")

    collect = commands.add_parser("collect", help="Collect all provided sources.")
    collect.add_argument("--source-list", required=True, type=Path)
    collect.add_argument("--token-file", type=Path)
    collect.add_argument("--output", required=True, type=Path)
    collect.add_argument("--vk-posts-limit", type=int, default=MAX_POSTS)

    validate = commands.add_parser("validate", help="Validate source_review.json.")
    validate.add_argument("--input", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "save-token":
            token = getpass.getpass("VK API token (hidden): ")
            path = save_token(
                args.workspace_root,
                token,
                token_name=args.token_name,
                force=args.force,
            )
            print(f"VK token saved securely: {path.name}")
        elif args.command == "collect":
            source_value = load_json(args.source_list)
            entries = normalize_source_list(source_value)
            token = read_token(args.token_file) if args.token_file else None
            result = collect_sources(
                entries,
                token,
                posts_limit=args.vk_posts_limit,
            )
            if args.output.exists():
                raise SourceError("Output already exists; use a new run directory.")
            atomic_write_json(args.output, result)
            print(
                f"Collected {len(result['sources'])} sources; "
                f"status: {result['status']}"
            )
        else:
            errors = validate_source_review(load_json(args.input))
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 1
            print(f"VALID: {args.input}")
    except (SourceError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
