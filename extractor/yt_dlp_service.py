"""
Core yt-dlp extraction service.

Responsibilities:
  - yt-dlp configuration per platform
  - extraction (info dict only, no file download)
  - format normalization
  - metadata normalization
  - platform detection
  - error mapping

The FastAPI route layer must NOT call yt-dlp directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

import yt_dlp

from .errors import (
    ExtractionException,
    InvalidURLError,
    classify_yt_dlp_error,
    ExtractionTimeoutError,
)
from .models import (
    CarouselEntry,
    ExtractionResult,
    MediaFormat,
    MediaType,
)

logger = logging.getLogger("extractor.service")

# ── Thread pool for yt-dlp (blocking I/O must not run in the async event loop) ──
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="yt_dlp")

# Extraction timeout (seconds). Adjust upward if very slow platforms are needed.
_EXTRACTION_TIMEOUT = 60


# ── Platform detection ───────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    """Best-effort platform name from URL. Falls back to 'generic'."""
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "instagram.com" in u:
        return "instagram"
    if "facebook.com" in u or "fb.watch" in u or "fb.com" in u:
        return "facebook"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "tiktok.com" in u:
        return "tiktok"
    return "generic"


# ── URL validation ───────────────────────────────────────────────────────────

def validate_url(url: str) -> None:
    """
    Reject obviously invalid or dangerous URLs before passing to yt-dlp.
    Only HTTP/HTTPS are accepted.
    """
    if not url or not url.strip():
        raise InvalidURLError("URL must not be empty")
    try:
        parsed = urlparse(url.strip())
    except Exception:
        raise InvalidURLError("Malformed URL")
    if parsed.scheme not in ("http", "https"):
        raise InvalidURLError("Only HTTP and HTTPS URLs are supported")
    if not parsed.netloc:
        raise InvalidURLError("URL has no host")


# ── yt-dlp option builders ───────────────────────────────────────────────────

def _build_base_options() -> dict:
    """Shared base yt-dlp options for all platforms."""
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,      # metadata extraction only — no file download
        "extract_flat": False,
        "socket_timeout": 30,
    }


def build_youtube_options() -> dict:
    """
    YouTube-specific yt-dlp options.

    Isolated here so YouTube configuration can be changed without touching
    any other code. Do not hardcode a permanent client choice — keep it easy
    to swap when YouTube changes its extraction requirements.

    Current approach:
      - Use yt-dlp defaults (let yt-dlp pick the best working client)
      - Do NOT force android/android_creator permanently
      - Do NOT hardcode any PO token
      - Do NOT restrict format selection at extraction time
        (Android picks quality; server returns all available formats)
    """
    opts = _build_base_options()
    # Let yt-dlp choose the client. Only add extractor_args if a specific
    # workaround is required after observing an actual failure.
    # Uncomment and adjust only when the specific error is confirmed:
    #
    # opts["extractor_args"] = {
    #     "youtube": {
    #         "player_client": ["android", "web"],
    #     }
    # }
    return opts


def _build_options(platform: str, cookie_file: Optional[str] = None) -> dict:
    """Assemble final yt-dlp options for a given platform."""
    if platform == "youtube":
        opts = build_youtube_options()
    else:
        opts = _build_base_options()

    # Platform-specific headers (never override UA for YouTube — yt-dlp
    # sets the correct UA per Innertube client internally)
    if platform == "instagram":
        opts["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.instagram.com/",
            "Accept-Language": "en-US,en;q=0.9",
        }
    elif platform in ("facebook",):
        opts["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.facebook.com/",
            "Accept-Language": "en-US,en;q=0.9",
        }
    elif platform == "tiktok":
        opts["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
            "Accept-Language": "en-US,en;q=0.9",
        }
    elif platform not in ("youtube",):
        opts["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

    if cookie_file:
        opts["cookiefile"] = cookie_file

    return opts


# ── Format normalization ─────────────────────────────────────────────────────

def _normalize_format(fmt: dict) -> MediaFormat:
    """
    Convert one yt-dlp format dict to a normalized MediaFormat.

    Key rule: do NOT assume a format has both video and audio just because
    it has a URL. Inspect vcodec/acodec explicitly.
    """
    vcodec = fmt.get("vcodec") or "none"
    acodec = fmt.get("acodec") or "none"

    has_video = vcodec not in ("none", "")
    has_audio = acodec not in ("none", "")

    height = fmt.get("height")
    if height and has_video:
        quality_label = f"{height}p"
    elif has_audio and not has_video:
        abr = fmt.get("abr")
        quality_label = f"audio {int(abr)}kbps" if abr else "audio only"
    else:
        quality_label = None

    return MediaFormat(
        format_id=str(fmt.get("format_id", "")),
        ext=fmt.get("ext"),
        width=fmt.get("width"),
        height=height,
        fps=fmt.get("fps"),
        filesize=fmt.get("filesize"),
        filesize_approx=fmt.get("filesize_approx"),
        vcodec=vcodec if has_video else None,
        acodec=acodec if has_audio else None,
        abr=fmt.get("abr"),
        vbr=fmt.get("vbr"),
        tbr=fmt.get("tbr"),
        protocol=fmt.get("protocol"),
        url=fmt.get("url"),
        has_video=has_video,
        has_audio=has_audio,
        downloadable_directly=True,  # always true for extracted URLs
        quality_label=quality_label,
    )


def _normalize_formats(info: dict) -> list[MediaFormat]:
    """Extract and normalize all formats from an info dict."""
    raw_formats = info.get("formats") or []
    result = []
    for fmt in raw_formats:
        if not fmt.get("url"):
            continue
        result.append(_normalize_format(fmt))
    # If no formats list but top-level url exists, synthesize one entry
    if not result and info.get("url"):
        vcodec = info.get("vcodec") or "none"
        acodec = info.get("acodec") or "none"
        result.append(MediaFormat(
            format_id=info.get("format_id", "0"),
            ext=info.get("ext"),
            width=info.get("width"),
            height=info.get("height"),
            fps=info.get("fps"),
            filesize=info.get("filesize"),
            filesize_approx=info.get("filesize_approx"),
            vcodec=vcodec if vcodec != "none" else None,
            acodec=acodec if acodec != "none" else None,
            url=info.get("url"),
            has_video=vcodec not in ("none", ""),
            has_audio=acodec not in ("none", ""),
            downloadable_directly=True,
        ))
    return result


def _detect_media_type(info: dict, formats: list[MediaFormat]) -> MediaType:
    """Infer the overall media type from the info dict and normalized formats."""
    # If any format has video it is a video post
    if any(f.has_video for f in formats):
        return MediaType.VIDEO
    if any(f.has_audio for f in formats):
        return MediaType.AUDIO

    # Fallback: check top-level vcodec
    vcodec = info.get("vcodec") or "none"
    if vcodec != "none":
        return MediaType.VIDEO

    # Instagram images return a direct URL with no formats
    ext = (info.get("ext") or "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif"):
        return MediaType.IMAGE

    return MediaType.UNKNOWN


def _normalize_carousel_entry(entry: dict) -> CarouselEntry:
    """Normalize one entry from a playlist / carousel."""
    vcodec = entry.get("vcodec") or "none"
    acodec = entry.get("acodec") or "none"
    has_video = vcodec not in ("none", "")

    if has_video:
        media_type = MediaType.VIDEO
        # Pick the best combined URL for the carousel item
        url = _pick_best_url(entry)
    else:
        media_type = MediaType.IMAGE
        url = entry.get("url") or entry.get("thumbnail") or ""

    ext = entry.get("ext")
    if not ext:
        ext = "mp4" if has_video else "jpg"

    return CarouselEntry(
        id=entry.get("id"),
        media_type=media_type,
        url=url,
        thumbnail=entry.get("thumbnail") or "",
        width=entry.get("width"),
        height=entry.get("height"),
        ext=ext,
        title=entry.get("title"),
        duration=entry.get("duration"),
    )


def _pick_best_url(info: dict) -> Optional[str]:
    """
    Pick the best direct URL for a single item.

    Priority:
      1. Combined format (has_video AND has_audio), highest height
      2. Any format with a URL
      3. Top-level info["url"]
    """
    formats = info.get("formats") or []

    # Combined formats
    combined = [
        f for f in formats
        if f.get("url")
        and (f.get("vcodec") or "none") not in ("none", "")
        and (f.get("acodec") or "none") not in ("none", "")
    ]
    if combined:
        combined.sort(key=lambda f: f.get("height") or 0, reverse=True)
        return combined[0]["url"]

    # Any format with URL
    any_with_url = [f for f in formats if f.get("url")]
    if any_with_url:
        any_with_url.sort(key=lambda f: f.get("height") or 0, reverse=True)
        return any_with_url[0]["url"]

    return info.get("url")


# ── Info dict → ExtractionResult ─────────────────────────────────────────────

def _build_result(info: dict, request_id: str, platform: str) -> ExtractionResult:
    """Convert a yt-dlp info dict to a normalized ExtractionResult."""
    entries_raw = info.get("entries") or []

    if entries_raw:
        # Playlist / carousel
        entries = [_normalize_carousel_entry(e) for e in entries_raw if e]
        media_type = (
            MediaType.VIDEO
            if any(e.media_type == MediaType.VIDEO for e in entries)
            else MediaType.IMAGE
        )
        return ExtractionResult(
            success=True,
            request_id=request_id,
            platform=platform,
            media_type=media_type,
            id=info.get("id"),
            title=info.get("title") or info.get("description") or "",
            description=info.get("description"),
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail=info.get("thumbnail") or "",
            duration=info.get("duration"),
            webpage_url=info.get("webpage_url") or info.get("original_url"),
            formats=[],
            entries=entries,
        )

    # Single item
    formats = _normalize_formats(info)
    media_type = _detect_media_type(info, formats)

    return ExtractionResult(
        success=True,
        request_id=request_id,
        platform=platform,
        media_type=media_type,
        id=info.get("id"),
        title=info.get("title") or info.get("description") or "",
        description=info.get("description"),
        uploader=info.get("uploader") or info.get("channel"),
        thumbnail=info.get("thumbnail") or "",
        duration=info.get("duration"),
        webpage_url=info.get("webpage_url") or info.get("original_url"),
        formats=formats,
        entries=[],
    )


# ── Blocking extraction (runs in thread pool) ─────────────────────────────────

def _run_extraction(url: str, opts: dict) -> dict:
    """
    Blocking yt-dlp call. Must be executed in a thread, not in the event loop.
    """
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# ── Public API ────────────────────────────────────────────────────────────────

async def extract(
    url: str,
    request_id: str,
    cookie_file: Optional[str] = None,
    platform: Optional[str] = None,
) -> ExtractionResult:
    """
    Async extraction entry point.

    Validates the URL, builds yt-dlp options, runs extraction in a thread pool
    (so the event loop is not blocked), normalizes the result, and maps any
    yt-dlp error to a structured ExtractionException.
    """
    validate_url(url)

    if platform is None:
        platform = detect_platform(url)

    opts = _build_options(platform, cookie_file)

    logger.info(
        "[request_id=%s] extraction started | platform=%s | url_host=%s",
        request_id, platform, urlparse(url).netloc,
    )
    t0 = time.monotonic()

    loop = asyncio.get_event_loop()
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(_EXECUTOR, _run_extraction, url, opts),
            timeout=_EXTRACTION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[request_id=%s] extraction timed out after %ds | platform=%s",
            request_id, _EXTRACTION_TIMEOUT, platform,
        )
        raise ExtractionTimeoutError()
    except ExtractionException:
        raise
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.warning(
            "[request_id=%s] extraction failed in %.1fs | platform=%s | error_type=%s",
            request_id, elapsed, platform, type(exc).__name__,
        )
        raise classify_yt_dlp_error(exc) from exc

    elapsed = time.monotonic() - t0
    fmt_count = len(info.get("formats") or []) if info else 0
    entry_count = len(info.get("entries") or []) if info else 0
    logger.info(
        "[request_id=%s] extraction completed in %.1fs | platform=%s | formats=%d | entries=%d",
        request_id, elapsed, platform, fmt_count, entry_count,
    )

    if not info:
        raise ExtractionTimeoutError("Extraction returned empty result")

    return _build_result(info, request_id, platform)
