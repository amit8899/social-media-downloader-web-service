"""
GET /diagnostics — development/testing endpoint.

Reports yt-dlp version, Python version, and tool availability.

IMPORTANT: Remove or protect this endpoint before exposing the API publicly.
It must NOT expose:
  - cookies or credentials
  - filesystem paths
  - environment variables
  - sensitive configuration
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import yt_dlp

router = APIRouter()


def _check_binary(name: str) -> bool:
    """Return True if a binary exists in PATH."""
    return shutil.which(name) is not None


def _get_binary_version(name: str, flag: str = "--version") -> str | None:
    """Run `name --version` and return the first line, or None on failure."""
    try:
        result = subprocess.run(
            [name, flag],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    except Exception:
        return None


@router.get("/diagnostics")
async def diagnostics():
    """
    Development-only diagnostics endpoint.
    Remove or protect before production deployment.
    """
    ffmpeg_ok = _check_binary("ffmpeg")
    ffprobe_ok = _check_binary("ffprobe")

    deno_ok = _check_binary("deno")
    node_ok = _check_binary("node")
    js_runtime = "deno" if deno_ok else ("node" if node_ok else None)

    # yt-dlp-ejs availability: check if the module is importable
    try:
        import yt_dlp_ejs  # noqa: F401
        ejs_available = True
    except ImportError:
        ejs_available = False

    return JSONResponse(content={
        "status": "ok",
        "yt_dlp_version": yt_dlp.version.__version__,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "ffmpeg_available": ffmpeg_ok,
        "ffmpeg_version": _get_binary_version("ffmpeg", "-version"),
        "ffprobe_available": ffprobe_ok,
        "javascript_runtime": js_runtime,
        "deno_version": _get_binary_version("deno") if deno_ok else None,
        "node_version": _get_binary_version("node") if node_ok else None,
        "ejs_available": ejs_available,
    })
