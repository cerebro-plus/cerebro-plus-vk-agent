from __future__ import annotations

import hashlib
import html
import importlib.util
import json
import math
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any, Callable

from common import MediaGateError


REQUIRED_MODULES = ("PIL", "yt_dlp", "faster_whisper")


def prepare_media_environment(
    run_dir: Path,
    module_lookup: Callable[[str], Any] = importlib.util.find_spec,
    executable_lookup: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    directories = [
        run_dir / "cache" / "images",
        run_dir / "cache" / "clips",
        run_dir / "cache" / "whisper",
        run_dir / "media" / "images",
        run_dir / "media" / "collages",
        run_dir / "media" / "clips",
        run_dir / "outputs",
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
    missing = [name for name in REQUIRED_MODULES if module_lookup(name) is None]
    report = {
        "ready": not missing,
        "missing_python_modules": missing,
        "ffmpeg": executable_lookup("ffmpeg"),
        "whisper_minimum_model": "small",
        "vad_required": True,
        "directories": [path.relative_to(run_dir).as_posix() for path in directories],
    }
    if missing:
        raise MediaGateError(
            "Missing media dependencies: " + ", ".join(sorted(missing))
        )
    return report


def choose_image_size(sizes: list[dict[str, Any]], maximum: int = 1600) -> dict[str, Any] | None:
    candidates = [
        item
        for item in sizes
        if item.get("url") and int(item.get("width") or 0) and int(item.get("height") or 0)
    ]
    if not candidates:
        return None
    within = [
        item
        for item in candidates
        if max(int(item["width"]), int(item["height"])) <= maximum
    ]
    if within:
        return max(within, key=lambda item: int(item["width"]) * int(item["height"]))
    return min(candidates, key=lambda item: max(int(item["width"]), int(item["height"])))


def cached_download(
    url: str,
    cache_dir: Path,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[Path, bool]:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mkv", ".webm"}:
        suffix = ".bin"
    destination = cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + suffix)
    if destination.is_file() and destination.stat().st_size:
        return destination, True
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener(request, timeout=90) as response:
        destination.write_bytes(response.read())
    return destination, False


def normalize_image(source: Path, destination: Path, maximum: int = 1600) -> tuple[int, int]:
    from PIL import Image, ImageOps

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
        image.save(destination, "JPEG", quality=91, optimize=True)
        return image.size


def make_collage(paths: list[Path], destination: Path) -> tuple[int, int]:
    from PIL import Image, ImageOps

    if not paths:
        raise MediaGateError("Cannot create an empty collage")
    destination.parent.mkdir(parents=True, exist_ok=True)
    images = []
    for path in paths:
        with Image.open(path) as opened:
            images.append(ImageOps.exif_transpose(opened).convert("RGB").copy())
    if len(images) == 1:
        images[0].save(destination, "JPEG", quality=91, optimize=True)
        return images[0].size
    cols = 2 if len(images) <= 4 else 3
    rows = math.ceil(len(images) / cols)
    tile_w, tile_h, gap = 500, 500, 12
    canvas = Image.new(
        "RGB",
        (cols * tile_w + gap * (cols - 1), rows * tile_h + gap * (rows - 1)),
        "white",
    )
    for index, image in enumerate(images):
        image.thumbnail((tile_w, tile_h), Image.Resampling.LANCZOS)
        col, row = index % cols, index // cols
        x = col * (tile_w + gap) + (tile_w - image.width) // 2
        y = row * (tile_h + gap) + (tile_h - image.height) // 2
        canvas.paste(image, (x, y))
    canvas.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    canvas.save(destination, "JPEG", quality=91, optimize=True)
    return canvas.size


def image_candidates(post: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    seen_urls: set[str] = set()

    def append_selected(kind: str, sizes: list[dict[str, Any]]) -> None:
        selected = choose_image_size(sizes)
        if not selected:
            return
        url = str(selected["url"])
        if url in seen_urls:
            return
        seen_urls.add(url)
        result.append({"kind": kind, **selected})

    def visit_structured(value: Any, kind: str) -> None:
        if isinstance(value, list):
            if value and all(isinstance(item, dict) for item in value):
                url_items = [
                    item
                    for item in value
                    if item.get("url")
                    and int(item.get("width") or 0)
                    and int(item.get("height") or 0)
                ]
                if url_items:
                    append_selected(kind, url_items)
                    return
            for item in value:
                visit_structured(item, kind)
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key in {"sizes", "images", "first_frame"} and isinstance(child, list):
                visit_structured(child, kind)
            elif key not in {"video", "files", "player"}:
                visit_structured(child, kind)

    for attachment in post.get("attachments") or []:
        attachment_type = str(attachment.get("type") or "").lower()
        if attachment_type == "photo":
            photo = attachment.get("photo") or {}
            append_selected("photo", photo.get("sizes") or [])
        elif attachment_type in {
            "carousel",
            "pretty_cards",
            "photos_list",
            "album",
            "market_album",
        }:
            visit_structured(
                attachment.get(attachment_type) or attachment,
                f"structured_{attachment_type}",
            )
    if post.get("post_type") == "Клип":
        for video in post.get("verified_videos") or []:
            before = len(result)
            append_selected("clip_thumbnail", video.get("image") or [])
            if len(result) > before:
                break
    return result


def direct_video_source(files: dict[str, Any]) -> tuple[str | None, str | None]:
    mp4 = []
    for key, value in files.items():
        if key.startswith("mp4_") and value:
            match = re.search(r"mp4_(\d+)", key)
            mp4.append((int(match.group(1)) if match else 0, str(value)))
    if mp4:
        mp4.sort(reverse=True)
        return mp4[0][1], "mp4"
    if files.get("hls"):
        return str(files["hls"]), "hls"
    if files.get("dash"):
        return str(files["dash"]), "dash"
    return None, None


def player_url_from_oembed(response: dict[str, Any] | None) -> str | None:
    markup = html.unescape(str((response or {}).get("html") or ""))
    match = re.search(r'src=["\']([^"\']+)["\']', markup)
    return match.group(1) if match else None


def files_from_player_html(page: str) -> dict[str, Any]:
    marker = re.search(r'"files"\s*:\s*', page)
    if marker:
        try:
            value, _ = json.JSONDecoder().raw_decode(page[marker.end() :])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    match = re.search(
        r'"files"\s*:\s*(\{.*?\})\s*,\s*"timeline_thumbs"',
        page,
        flags=re.DOTALL,
    )
    if not match:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def download_confirmed_clip(
    client: Any,
    post_id: str,
    video: dict[str, Any],
    run_dir: Path,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    owner_id = int(video.get("owner_id") or 0)
    video_id = int(video.get("video_id") or 0)
    key = f"{owner_id}_{video_id}"
    if video.get("access_key"):
        key += f"_{video['access_key']}"
    try:
        response = client.call("video.get", {"videos": key, "extended": 0})
    except Exception:
        response = {}
    item = ((response or {}).get("items") or [{}])[0]
    resolved_type = str(item.get("type") or video.get("inner_type") or "").lower()
    if not video.get("is_clip") and resolved_type not in {"clip", "short_video"}:
        return {"status": "unavailable", "reason": "video.get did not confirm clip type"}
    url, source_type = direct_video_source(item.get("files") or video.get("files") or {})
    source = "video.get.files"
    if not url:
        oembed = client.call(
            "video.getOembed", {"url": f"https://vk.com/video{owner_id}_{video_id}"}
        )
        player_url = player_url_from_oembed(oembed)
        if not player_url:
            return {"status": "unavailable", "reason": "video.getOembed returned no player"}
        request = urllib.request.Request(player_url, headers={"User-Agent": "Mozilla/5.0"})
        with opener(request, timeout=45) as page_response:
            page = page_response.read().decode("utf-8", "replace")
        url, source_type = direct_video_source(files_from_player_html(page))
        source = "video.getOembed.public_player"
    if not url:
        return {"status": "unavailable", "reason": "No MP4/HLS/DASH source"}
    if source_type != "mp4":
        try:
            import yt_dlp
        except ImportError:
            return {"status": "unavailable", "reason": "yt-dlp is unavailable"}
        destination = run_dir / "media" / "clips" / f"{post_id.replace('-', 'm')}.mp4"
        options = {
            "outtmpl": str(destination),
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
        except Exception as exc:
            return {"status": "unavailable", "reason": f"yt-dlp failed: {exc}"}
    else:
        cached, _ = cached_download(url, run_dir / "cache" / "clips", opener)
        destination = run_dir / "media" / "clips" / f"{post_id.replace('-', 'm')}.mp4"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cached, destination)
    return {
        "status": "downloaded",
        "path": destination.relative_to(run_dir).as_posix(),
        "source": source,
    }


def clean_transcript(parts: list[str]) -> str:
    text = re.sub(r"\s+", " ", " ".join(item.strip() for item in parts if item.strip())).strip()
    text = re.sub(r"\b([\wёЁ-]{2,})\s+\1\b", r"\1", text, flags=re.I)
    text = re.sub(r"\b[\wёЁ]+-(?=\s|[.!?]|$)", "[неразборчиво]", text)
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?…" else text + "."


def transcribe_clip(
    clip_path: Path,
    model_dir: Path,
    model_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if model_factory is None:
        from faster_whisper import WhisperModel

        model_factory = WhisperModel
    model = model_factory(
        "small", device="cpu", compute_type="int8", download_root=str(model_dir)
    )
    try:
        segments_iter, info = model.transcribe(
            str(clip_path),
            beam_size=5,
            temperature=0.0,
            language="ru",
            task="transcribe",
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 250},
        )
        segments = list(segments_iter)
        audible = [
            item
            for item in segments
            if str(item.text).strip() and float(item.no_speech_prob or 0.0) < 0.75
        ]
        if not audible:
            return {
                "status": "no_speech",
                "transcript": "Речь отсутствует.",
                "model": "small",
                "vad": True,
            }
        transcript = clean_transcript(
            [
                "[неразборчиво]"
                if float(item.avg_logprob or 0.0) < -2.0
                else str(item.text)
                for item in audible
            ]
        )
        return {
            "status": "success",
            "transcript": transcript or "[неразборчиво]",
            "model": "small",
            "vad": True,
            "language": "ru",
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "transcript": "Видео недоступно для локальной расшифровки.",
            "model": "small",
            "vad": True,
            "error": f"{type(exc).__name__}: {exc}",
        }


def evaluate_media_gate(
    summary: dict[str, Any],
    allow_degraded: bool = False,
    degraded_consent: str | None = None,
    media_root: Path | None = None,
) -> dict[str, Any]:
    reasons = []
    if int(summary.get("expected_images") or 0) > 0 and int(
        summary.get("downloaded_images") or 0
    ) == 0:
        reasons.append("expected_images_but_downloaded_zero")
    if int(summary.get("expected_collages") or 0) > int(
        summary.get("created_collages") or 0
    ):
        reasons.append("best_post_collage_missing")
    if int(summary.get("confirmed_clips") or 0) > 0 and int(
        summary.get("downloaded_clips") or 0
    ) == 0:
        reasons.append("confirmed_clips_but_downloaded_zero")
    for item in summary.get("clip_transcriptions") or []:
        if item.get("status") not in {"success", "no_speech"}:
            reasons.append(f"unprocessed_clip:{item.get('post_id')}")
        if item.get("status") == "success" and item.get("language") != "ru":
            reasons.append(f"non_russian_transcript:{item.get('post_id')}")
    for item in summary.get("media_paths") or []:
        path = Path(item)
        if not path.is_absolute() and media_root is not None:
            path = media_root / path
        if not path.is_file():
            reasons.append(f"missing_media_path:{item}")
    consent_valid = bool(allow_degraded and degraded_consent and degraded_consent.strip())
    if reasons and not consent_valid:
        raise MediaGateError("; ".join(reasons))
    return {
        "passed": not reasons,
        "degraded": bool(reasons),
        "degraded_consent_recorded": consent_valid,
        "reasons": reasons,
    }
