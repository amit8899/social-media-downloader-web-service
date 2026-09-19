import os
import json
import hashlib
import tempfile
import time
from collections import OrderedDict
from fastapi import FastAPI, Query, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# CORS – allow any origin so your Android app can reach it
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Simple in-memory LRU cache  (url → result, TTL = 60 min)
# ---------------------------------------------------------------------------
_CACHE_MAX = 200
_CACHE_TTL = 3600  # seconds

class _LRUCache:
    def __init__(self, maxsize: int, ttl: int):
        self._cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def _key(self, url: str, cookies: str) -> str:
        raw = f"{url}|{cookies or ''}"
        return hashlib.md5(raw.encode()).hexdigest()

    def get(self, url: str, cookies: str):
        key = self._key(url, cookies)
        if key in self._cache:
            ts, value = self._cache[key]
            if time.time() - ts < self._ttl:
                self._cache.move_to_end(key)
                return value
            else:
                del self._cache[key]
        return None

    def set(self, url: str, cookies: str, value: dict):
        key = self._key(url, cookies)
        self._cache[key] = (time.time(), value)
        self._cache.move_to_end(key)
        if len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

_cache = _LRUCache(_CACHE_MAX, _CACHE_TTL)


# ---------------------------------------------------------------------------
# Helper – write Netscape cookie file from raw "Cookie:" header string
# ---------------------------------------------------------------------------
def _write_cookie_file(cookie_header: str) -> str | None:
    """
    Converts the raw Cookie header value (the same string the browser sends,
    e.g.  "sessionid=abc; csrftoken=xyz; ds_user_id=123")
    into a Netscape-format cookies.txt file that yt-dlp can read.
    Returns the temp file path, or None if cookie_header is empty.
    """
    if not cookie_header or not cookie_header.strip():
        return None

    lines = ["# Netscape HTTP Cookie File\n"]
    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        # domain  subdom  path  secure  expiry  name  value
        # We apply cookies to both instagram.com and facebook.com domains
        for domain in [".instagram.com", ".facebook.com", ".fb.com"]:
            lines.append(
                f"{domain}\tTRUE\t/\tFALSE\t9999999999\t{name}\t{value}\n"
            )

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix="cookies_"
    )
    tmp.writelines(lines)
    tmp.flush()
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# /download  – returns JSON with media URL(s)
# ---------------------------------------------------------------------------
@app.get("/download")
async def download_media(
    url: str = Query(..., description="Public or private social media URL"),
    cookie: str = Header(
        default=None,
        alias="X-User-Cookie",
        description="Raw Cookie header string from user's browser session",
    ),
):
    """
    Returns JSON:
      {
        "url":        "<direct media URL>",   // first/best video or image
        "urls":       ["<url1>", "<url2>"],   // all URLs (carousel/album)
        "thumbnail":  "<thumb url>",
        "title":      "<post title>",
        "type":       "video" | "image",
        "from_cache": true | false
      }
    """
    # 1. Cache lookup
    cached = _cache.get(url, cookie or "")
    if cached:
        cached["from_cache"] = True
        return JSONResponse(content=cached, headers={"Cache-Control": "max-age=3600, public"})

    # 2. Build yt-dlp options
    cookie_file = _write_cookie_file(cookie)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,       # we only want the info dict, NOT the file
        "extract_flat": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.instagram.com/",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=422, detail=f"yt-dlp error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
    finally:
        if cookie_file:
            try:
                os.unlink(cookie_file)
            except OSError:
                pass

    if not info:
        raise HTTPException(status_code=404, detail="No media info extracted")

    # 3. Parse info dict → unified response
    result = _parse_info(info)

    # 4. Store in cache
    _cache.set(url, cookie or "", result)

    return JSONResponse(
        content={**result, "from_cache": False},
        headers={"Cache-Control": "max-age=3600, public"},
    )


def _best_video_url(info: dict) -> str | None:
    """Pick the best video URL from formats list."""
    formats = info.get("formats") or []
    # Prefer mp4 with both video+audio, pick highest height
    video_formats = [
        f for f in formats
        if f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and f.get("url")
    ]
    if not video_formats:
        # Fall back to any format that has a URL
        video_formats = [f for f in formats if f.get("url")]

    if video_formats:
        video_formats.sort(key=lambda f: f.get("height") or 0, reverse=True)
        return video_formats[0]["url"]

    return info.get("url")


def _parse_info(info: dict) -> dict:
    """Convert yt-dlp info dict to our simple response format."""
    # Handle playlists / carousels
    entries = info.get("entries")
    if entries:
        urls = []
        thumbs = []
        media_type = "image"
        for entry in entries:
            if not entry:
                continue
            if entry.get("vcodec") != "none" or entry.get("formats"):
                u = _best_video_url(entry) or entry.get("url", "")
                media_type = "video"
            else:
                u = entry.get("url") or entry.get("thumbnail") or ""
            urls.append(u)
            thumbs.append(entry.get("thumbnail") or "")

        return {
            "url": urls[0] if urls else "",
            "urls": urls,
            "thumbnail": thumbs[0] if thumbs else "",
            "thumbnails": thumbs,
            "title": info.get("title") or info.get("description") or "",
            "type": media_type,
        }

    # Single item
    vcodec = info.get("vcodec", "")
    is_video = vcodec and vcodec != "none"

    if is_video or info.get("formats"):
        media_url = _best_video_url(info) or ""
        media_type = "video"
    else:
        media_url = info.get("url") or info.get("thumbnail") or ""
        media_type = "image"

    thumbnail = info.get("thumbnail") or ""

    return {
        "url": media_url,
        "urls": [media_url] if media_url else [],
        "thumbnail": thumbnail,
        "thumbnails": [thumbnail] if thumbnail else [],
        "title": info.get("title") or info.get("description") or "",
        "type": media_type,
    }


# ---------------------------------------------------------------------------
# Health-check / keep-alive endpoint (prevents Render cold starts when pinged)
# ---------------------------------------------------------------------------
@app.get("/ping")
async def ping():
    return {"status": "ok", "timestamp": int(time.time())}


@app.get("/")
async def root():
    return {
        "message": (
            "Social Media Video Downloader API. "
            "GET /download?url=<url> — pass X-User-Cookie header for private content."
        )
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))