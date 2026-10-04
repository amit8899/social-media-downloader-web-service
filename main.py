"""
Social Media Downloader — FastAPI application entry point.

Architecture:
  Android → POST /extract → FastAPI → extractor service → yt-dlp → ExtractionResult → Android
                                                                   (extraction only, no file download)

Endpoints:
  GET  /health         Lightweight health check (does NOT call yt-dlp)
  GET  /ping           Keep-alive / Render cold-start prevention
  POST /extract        Normalized extraction (new, preferred)
  GET  /download       Legacy backward-compat endpoint (kept during migration)
  GET  /api/youtube/hls-formats  YouTube web_safari HLS extraction experiment
  GET  /diagnostics    Development-only: yt-dlp/FFmpeg/runtime diagnostics
  GET  /               API info root

See README.md for migration guide.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.request
import uuid
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import yt_dlp

from api.extract import router as extract_router
from api.diagnostics import router as diagnostics_router
from extractor import yt_dlp_service
from extractor.errors import ExtractionException, LoginRequiredError, classify_yt_dlp_error
from extractor.models import ExtractionError, ExtractionResult
from utils.cache import get_cache
from utils.cookies import create_cookie_file, delete_cookie_file
from utils.logging_config import configure_logging

# ── Initialise ────────────────────────────────────────────────────────────────
load_dotenv()
configure_logging(os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("main")

app = FastAPI(
    title="Social Media Downloader API",
    description=(
        "FastAPI + yt-dlp extraction service. "
        "Android downloads media directly from extracted CDN URLs — "
        "the server performs extraction only."
    ),
    version="2.0.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount routers ─────────────────────────────────────────────────────────────
app.include_router(extract_router)
app.include_router(diagnostics_router)


# ── Health / root ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """
    Lightweight health check — does NOT invoke yt-dlp.
    Use this for Render's health check path to keep it cheap.
    """
    return {"status": "ok"}


@app.get("/ping")
async def ping():
    """Keep-alive / Render cold-start prevention."""
    return {"status": "ok", "timestamp": int(time.time())}


@app.get("/")
async def root():
    return {
        "service": "Social Media Downloader API v2",
        "endpoints": {
            "POST /extract": "Normalized extraction (preferred)",
            "GET /download":  "Legacy endpoint (backward compat)",
            "GET /api/extractors/latest": "Latest dynamic Python extractor script",
            "GET /api/youtube/hls-formats": "YouTube web_safari HLS extraction experiment",
            "GET /api/youtube/po-token": "Guarded YouTube GVS PO-token helper",
            "GET /health":    "Health check",
            "GET /diagnostics": "Dev diagnostics (remove before production)",
        },
    }


# ── Dynamic Extractor Script Endpoint ─────────────────────────────────────────

EXTRACTOR_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "extract_video.py")
FACEBOOK_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "facebook_updated.py")
EXTRACTOR_VERSION = int(os.getenv("EXTRACTOR_VERSION", "17"))

@app.get("/api/extractors/latest")
async def get_latest_extractor():
    """
    Returns the latest dynamic extractor script and its version.
    Android apps fetch this at launch to run the latest extraction logic
    on the user's residential mobile IP without requiring an APK update.
    """
    if not os.path.exists(EXTRACTOR_SCRIPT_PATH):
        return JSONResponse(status_code=404, content={"error": "extract_video.py not found on server"})

    with open(EXTRACTOR_SCRIPT_PATH, "r", encoding="utf-8") as f:
        script_content = f.read()

    fb_content = ""
    if os.path.exists(FACEBOOK_SCRIPT_PATH):
        with open(FACEBOOK_SCRIPT_PATH, "r", encoding="utf-8") as f:
            fb_content = f.read()

    return {
        "version": EXTRACTOR_VERSION,
        "filename": "extract_video.py",
        "script": script_content,
        "facebook_script": fb_content,
        "timestamp": int(os.path.getmtime(EXTRACTOR_SCRIPT_PATH)),
    }


# ── Local bgutil provider lifecycle ───────────────────────────────────────────

_BGUTIL_PROCESS = None


def _bgutil_server_dir() -> str:
    return os.getenv(
        "BGUTIL_SERVER_DIR",
        os.path.join(os.path.dirname(__file__), "vendor", "bgutil-ytdlp-pot-provider", "server"),
    )


def _bgutil_ping_ok() -> bool:
    ping_url = os.getenv("BGUTIL_PING_URL", "http://127.0.0.1:4416/ping")
    try:
        req = urllib.request.Request(ping_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _ensure_bgutil_provider_running() -> None:
    global _BGUTIL_PROCESS
    if _bgutil_ping_ok():
        logger.info("bgutil PO provider already reachable on 127.0.0.1:4416")
        return

    server_dir = _bgutil_server_dir()
    entrypoint = os.path.join(server_dir, "build", "main.js")
    if not os.path.exists(entrypoint):
        logger.warning("bgutil PO provider entrypoint missing: %s", entrypoint)
        return

    try:
        logger.info("Starting bgutil PO provider from %s", server_dir)
        _BGUTIL_PROCESS = subprocess.Popen(
            ["node", "build/main.js", "--host", "127.0.0.1", "--port", "4416"],
            cwd=server_dir,
        )
        time.sleep(3)
        if _bgutil_ping_ok():
            logger.info("bgutil PO provider is reachable after startup")
        else:
            rc = _BGUTIL_PROCESS.poll() if _BGUTIL_PROCESS else None
            logger.warning("bgutil PO provider still unreachable after startup; process returncode=%s", rc)
    except Exception as exc:
        logger.warning("Failed to start bgutil PO provider: %s", exc)


@app.on_event("startup")
async def startup_bgutil_provider():
    _ensure_bgutil_provider_running()


@app.get("/api/youtube/po-token/status")
async def get_youtube_po_token_status(
    x_app_token: Optional[str] = Header(default=None, alias="X-App-Token"),
):
    expected_token = os.getenv("YOUTUBE_PO_API_TOKEN", "").strip()
    if not expected_token or not x_app_token or x_app_token.strip() != expected_token:
        return JSONResponse(status_code=401, content={"success": False, "error": "Unauthorized"})
    server_dir = _bgutil_server_dir()
    return {
        "success": True,
        "provider_reachable": _bgutil_ping_ok(),
        "server_dir": server_dir,
        "entrypoint_exists": os.path.exists(os.path.join(server_dir, "build", "main.js")),
        "process_returncode": _BGUTIL_PROCESS.poll() if _BGUTIL_PROCESS else None,
    }


# ── YouTube PO Token Helper ───────────────────────────────────────────────────

def _extract_youtube_video_id(url: str) -> str:
    if not url:
        return ""
    patterns = (
        r"(?:v=|/shorts/|/embed/|/live/)([0-9A-Za-z_-]{11})",
        r"youtu\.be/([0-9A-Za-z_-]{11})",
    )
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", url):
        return url
    return ""


def _request_bgutil_po_token(video_id: str) -> dict:
    provider_url = os.getenv("BGUTIL_PROVIDER_URL", "http://127.0.0.1:4416/get_pot")
    payload = json.dumps({
        "content_binding": video_id,
        "proxy": "",
        "bypass_cache": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        provider_url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=35) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    return json.loads(body or "{}")


@app.get("/api/youtube/po-token")
async def get_youtube_po_token(
    url: str = Query(..., description="YouTube URL or video ID"),
    x_app_token: Optional[str] = Header(default=None, alias="X-App-Token"),
):
    """
    Generates a GVS PO token for the requested YouTube video.
    Android still performs yt-dlp extraction and media download locally; this
    endpoint only supplies the proof-of-origin token helper data.
    """
    expected_token = os.getenv("YOUTUBE_PO_API_TOKEN", "").strip()
    if not expected_token:
        return JSONResponse(
            status_code=503,
            content={"success": False, "error": "PO token helper is not configured"},
        )
    if not x_app_token or x_app_token.strip() != expected_token:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "Unauthorized"},
        )

    video_id = _extract_youtube_video_id(url)
    if not video_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "Invalid YouTube URL or video ID"},
        )

    _ensure_bgutil_provider_running()

    try:
        data = _request_bgutil_po_token(video_id)
    except Exception as exc:
        logger.warning("YouTube PO token generation failed for %s: %s", video_id, exc)
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(exc), "video_id": video_id},
        )

    po_token = data.get("poToken") or data.get("po_token") or data.get("pot") or ""
    if not po_token:
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": "Provider returned no poToken", "video_id": video_id},
        )

    return {
        "success": True,
        "video_id": video_id,
        "client": "mweb",
        "scope": "gvs",
        "po_token": po_token,
    }


# ── YouTube HLS Extraction Experiment ─────────────────────────────────────────

@app.get("/api/youtube/hls-formats")
async def get_youtube_hls_formats(url: str = Query(..., description="YouTube URL")):
    """
    Extract web_safari HLS formats on the server where a JavaScript/EJS runtime
    is available. Android still downloads the returned m3u8 URLs directly.
    This is an experiment: some YouTube HLS URLs may still be IP/session bound.
    """
    opts = {
        "quiet": True,
        "no_warnings": False,
        "skip_download": True,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {
                "player_client": ["web_safari"],
                "player_skip": ["configs"],
                "formats": ["missing_pot"],
            }
        },
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.warning("YouTube HLS extraction failed: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"success": False, "error": str(exc), "formats": []},
        )

    hls_formats = []
    seen_urls = set()
    for fmt in info.get("formats") or []:
        fmt_url = fmt.get("url") or ""
        protocol = str(fmt.get("protocol") or "").lower()
        ext = str(fmt.get("ext") or "").lower()
        vcodec = str(fmt.get("vcodec") or "none").lower()
        if not fmt_url or fmt_url in seen_urls:
            continue
        if "m3u8" not in protocol and ".m3u8" not in fmt_url.lower() and ext != "m3u8":
            continue
        if "vp09" in vcodec or "vp9" in vcodec or "av01" in vcodec or "av1" in vcodec:
            continue
        seen_urls.add(fmt_url)
        hls_formats.append({
            "format_id": str(fmt.get("format_id") or "hls"),
            "url": fmt_url,
            "height": fmt.get("height") or 0,
            "width": fmt.get("width") or 0,
            "ext": "mp4",
            "protocol": fmt.get("protocol") or "m3u8_native",
            "vcodec": fmt.get("vcodec") or "h264",
            "acodec": fmt.get("acodec") or "aac",
            "filesize": fmt.get("filesize") or 0,
            "filesize_approx": fmt.get("filesize_approx") or 0,
            "tbr": fmt.get("tbr") or 0,
        })

    hls_formats.sort(key=lambda f: f.get("height") or 0, reverse=True)
    return {
        "success": True,
        "title": info.get("title") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
        "formats": hls_formats,
    }


# ── Legacy GET /download  (backward compatibility) ────────────────────────────
#
# This endpoint is kept so Android can continue working during the migration
# to POST /extract. Do not add new features here. Migrate Android to
# POST /extract and then remove this endpoint.

@app.get("/download")
async def download_media_legacy(
    url: str = Query(..., description="Social media URL"),
    x_user_cookie: Optional[str] = Header(default=None, alias="X-User-Cookie"),
):
    """
    Legacy extraction endpoint.

    Returns the same JSON shape as before so existing Android code continues
    to work without changes. New Android code should use POST /extract.

    Response shape (unchanged from v1):
      {
        "url":        "<best direct URL>",
        "urls":       ["<url1>", ...],
        "thumbnail":  "<thumb url>",
        "title":      "<title>",
        "type":       "video" | "image",
        "from_cache": true | false
      }
    """
    request_id = str(uuid.uuid4())
    cache = get_cache()
    has_cookies = bool(x_user_cookie)

    # Cache check
    cached = cache.get(url, has_cookies)
    if cached:
        legacy = _result_to_legacy(cached)
        legacy["from_cache"] = True
        return JSONResponse(content=legacy)

    platform = yt_dlp_service.detect_platform(url)
    cookie_file = None
    try:
        if x_user_cookie:
            cookie_file = create_cookie_file(x_user_cookie, platform)

        result: ExtractionResult = await yt_dlp_service.extract(
            url=url,
            request_id=request_id,
            cookie_file=cookie_file,
            platform=platform,
        )
    except ExtractionException as exc:
        status_code = 422 if isinstance(exc, LoginRequiredError) else 502
        return JSONResponse(
            content={"error": exc.message, "error_code": exc.code, "url": "", "urls": [], "from_cache": False},
            status_code=status_code,
        )
    except Exception:
        return JSONResponse(
            content={"error": "Unexpected server error", "url": "", "urls": [], "from_cache": False},
            status_code=500,
        )
    finally:
        delete_cookie_file(cookie_file)

    result_dict = result.model_dump(mode="json")
    cache.set(url, has_cookies, result_dict)

    legacy = _result_to_legacy(result_dict)
    legacy["from_cache"] = False
    return JSONResponse(content=legacy)


def _result_to_legacy(result: dict) -> dict:
    """
    Convert a normalized ExtractionResult dict to the v1 legacy response shape
    so Android v1 code continues to work unchanged.
    """
    if not result.get("success", True):
        err = (result.get("error") or {}).get("message", "Extraction failed")
        return {"url": "", "urls": [], "thumbnail": "", "title": "", "type": "video", "error": err}

    entries = result.get("entries") or []
    if entries:
        urls = [e.get("url") or "" for e in entries]
        thumbs = [e.get("thumbnail") or "" for e in entries]
        media_type = "video" if any(e.get("media_type") == "video" for e in entries) else "image"
        return {
            "url": urls[0] if urls else "",
            "urls": urls,
            "thumbnail": thumbs[0] if thumbs else "",
            "thumbnails": thumbs,
            "title": result.get("title") or "",
            "type": media_type,
            "filesize": 0,
            "duration": result.get("duration") or 0,
        }

    formats = result.get("formats") or []

    # Pick best combined URL for the legacy single-URL field
    combined = [f for f in formats if f.get("has_video") and (f.get("has_audio") or f.get("audio_url")) and f.get("url")]
    if not combined:
        combined = [f for f in formats if f.get("url")]
    combined.sort(key=lambda f: f.get("height") or 0, reverse=True)
    best_url = combined[0]["url"] if combined else ""

    all_urls = [f["url"] for f in formats if f.get("url")]
    media_type = result.get("media_type") or "unknown"
    if media_type not in ("video", "image", "audio"):
        media_type = "video"

    best_fmt = combined[0] if combined else {}
    return {
        "url": best_url,
        "urls": all_urls,
        "thumbnail": result.get("thumbnail") or "",
        "thumbnails": [result.get("thumbnail")] if result.get("thumbnail") else [],
        "title": result.get("title") or "",
        "type": media_type,
        "filesize": best_fmt.get("filesize") or best_fmt.get("filesize_approx") or 0,
        "duration": result.get("duration") or 0,
        "formats": formats,
    }


# ── Dev entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=False,
    )