"""
Cookie manager: create/destroy temporary Netscape cookie files.

Rules:
  - Cookies are never logged
  - Cookies are never stored beyond one extraction request
  - The temp file is always deleted in a finally block
  - Cookies are never included in API responses or error messages
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional


_DOMAIN_MAP: dict[str, list[str]] = {
    "youtube":   [".youtube.com", "youtube.com", ".google.com", "google.com"],
    "instagram": [".instagram.com", "instagram.com"],
    "facebook":  [".facebook.com", "facebook.com", ".fb.com", "fb.com"],
    "twitter":   [".twitter.com", "twitter.com", ".x.com", "x.com"],
    "tiktok":    [".tiktok.com", "tiktok.com"],
}


def create_cookie_file(cookie_header: str, platform: str) -> Optional[str]:
    """
    Convert a raw Cookie header string (``k=v; k2=v2``) into a Netscape
    cookie file that yt-dlp can parse.

    Returns the path of the temp file, or None if cookie_header is empty.
    The caller is responsible for deleting the file via :func:`delete_cookie_file`.
    """
    if not cookie_header or not cookie_header.strip():
        return None

    domains = _DOMAIN_MAP.get(platform, [f".{platform}.com", f"{platform}.com"])
    expiry = "2147483647"

    lines = [
        "# Netscape HTTP Cookie File\n",
        "# https://curl.haxx.se/rfc/cookie_spec.html\n",
    ]

    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        for domain in domains:
            include_sub = "TRUE" if domain.startswith(".") else "FALSE"
            lines.append(
                f"{domain}\t{include_sub}\t/\tTRUE\t{expiry}\t{name}\t{value}\n"
            )

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, prefix="ck_"
    )
    tmp.writelines(lines)
    tmp.flush()
    tmp.close()
    return tmp.name


def delete_cookie_file(path: Optional[str]) -> None:
    """Safely delete a temporary cookie file. Never raises."""
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass
