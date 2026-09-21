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
  GET  /diagnostics    Development-only: yt-dlp/FFmpeg/runtime diagnostics
  GET  /               API info root

See README.md for migration guide.
"""
from __future__ import annotations

import logging
import os
import time
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
from extractor.errors import ExtractionException, classify_yt_dlp_error
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
            "GET /health":    "Health check",
            "GET /diagnostics": "Dev diagnostics (remove before production)",
        },
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
        return JSONResponse(
            content={"error": exc.message, "url": "", "urls": [], "from_cache": False},
            status_code=502,
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
    combined = [f for f in formats if f.get("has_video") and f.get("has_audio") and f.get("url")]
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