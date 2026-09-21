"""
In-memory LRU cache for extraction results.

Design decisions:
  - Metadata (titles, format lists) is cached — that is stable.
  - Extracted direct media URLs (CDN-signed, time-limited) are cached with
    a conservative TTL (20 minutes). Android must be able to request a fresh
    extraction if a cached URL has expired server-side.
  - Cookies are NEVER used as cache keys in plain text. The key uses a
    SHA-256 digest of (url + cookie_presence_flag) only.
  - No distributed cache — this is an in-memory prototype.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Optional


# Direct media URLs from YouTube are typically valid for ~6 hours,
# Instagram for ~1 hour. Use a conservative TTL that forces re-extraction
# before URLs are likely to have expired.
_CACHE_TTL_SECONDS = 20 * 60   # 20 minutes
_CACHE_MAX_ENTRIES = 200


class ExtractionCache:
    def __init__(self, maxsize: int = _CACHE_MAX_ENTRIES, ttl: int = _CACHE_TTL_SECONDS):
        self._store: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def _make_key(self, url: str, has_cookies: bool) -> str:
        """
        Key = SHA-256(url + cookie_presence_flag).
        Cookie content is never stored in the key.
        """
        raw = f"{url}|{'1' if has_cookies else '0'}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, url: str, has_cookies: bool) -> Optional[dict]:
        key = self._make_key(url, has_cookies)
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts >= self._ttl:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value

    def set(self, url: str, has_cookies: bool, value: dict) -> None:
        key = self._make_key(url, has_cookies)
        self._store[key] = (time.monotonic(), value)
        self._store.move_to_end(key)
        while len(self._store) > self._maxsize:
            self._store.popitem(last=False)

    def invalidate(self, url: str, has_cookies: bool) -> None:
        """Force re-extraction for a URL on next request."""
        key = self._make_key(url, has_cookies)
        self._store.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._store)


# Module-level singleton
_cache = ExtractionCache()


def get_cache() -> ExtractionCache:
    return _cache
