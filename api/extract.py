"""
POST /extract  — normalized extraction endpoint.

The route layer is responsible for:
  - generating / forwarding request ID
  - validating the request body
  - calling the extractor service
  - converting extractor exceptions to HTTP responses

The route layer must NOT call yt-dlp directly.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from extractor.errors import ExtractionException, InvalidURLError, UnsupportedURLError
from extractor.models import ExtractionError, ExtractionResult
from extractor import yt_dlp_service
from utils.cache import get_cache
from utils.cookies import create_cookie_file, delete_cookie_file

logger = logging.getLogger("api.extract")

router = APIRouter()


# ── Request / response models ─────────────────────────────────────────────────

class ExtractRequest(BaseModel):
    url: str
    platform: Optional[str] = None   # auto-detected if omitted
    force_refresh: bool = False       # set true to bypass cache


# ── POST /extract ─────────────────────────────────────────────────────────────

@router.post("/extract", response_model=ExtractionResult)
async def extract_media(
    body: ExtractRequest,
    x_request_id: Optional[str] = Header(default=None, alias="X-Request-ID"),
    x_user_cookie: Optional[str] = Header(default=None, alias="X-User-Cookie"),
):
    """
    Extract metadata and format list for any yt-dlp-supported URL.

    Returns a normalized ExtractionResult. Never returns raw yt-dlp objects.
    Video-only and audio-only formats are represented separately from
    combined formats — Android must not assume a format contains both streams
    unless has_video=true AND has_audio=true.

    On error returns a structured error object with a normalized code,
    never a raw yt-dlp traceback.
    """
    request_id = x_request_id or str(uuid.uuid4())

    # ── Cache check ───────────────────────────────────────────────────────────
    cache = get_cache()
    has_cookies = bool(x_user_cookie)

    if not body.force_refresh:
        cached = cache.get(body.url, has_cookies)
        if cached:
            logger.info("[request_id=%s] cache hit | url_prefix=%s",
                        request_id, body.url[:50])
            cached["request_id"] = request_id
            cached["from_cache"] = True
            return JSONResponse(content=cached)

    # ── Cookie temp file ──────────────────────────────────────────────────────
    platform = body.platform or yt_dlp_service.detect_platform(body.url)
    cookie_file = None
    try:
        if x_user_cookie:
            cookie_file = create_cookie_file(x_user_cookie, platform)

        # ── Extract ───────────────────────────────────────────────────────────
        result: ExtractionResult = await yt_dlp_service.extract(
            url=body.url,
            request_id=request_id,
            cookie_file=cookie_file,
            platform=platform,
        )

    except InvalidURLError as exc:
        return _error_response(request_id, exc, status_code=400)

    except UnsupportedURLError as exc:
        return _error_response(request_id, exc, status_code=422)

    except ExtractionException as exc:
        return _error_response(request_id, exc, status_code=502)

    except Exception as exc:
        logger.error("[request_id=%s] unhandled error: %s", request_id, type(exc).__name__)
        return _error_response(
            request_id,
            ExtractionException("SERVER_ERROR", "An unexpected server error occurred", retryable=True),
            status_code=500,
        )

    finally:
        delete_cookie_file(cookie_file)

    # ── Cache store (metadata only — CDN URLs have limited lifetime) ──────────
    result_dict = result.model_dump(mode="json")
    result_dict["from_cache"] = False
    cache.set(body.url, has_cookies, result_dict)

    return JSONResponse(content=result_dict)


def _error_response(request_id: str, exc: ExtractionException, status_code: int) -> JSONResponse:
    result = ExtractionResult(
        success=False,
        request_id=request_id,
        error=ExtractionError(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
        ),
    )
    return JSONResponse(
        content=result.model_dump(mode="json"),
        status_code=status_code,
    )
