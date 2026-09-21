#!/usr/bin/env python3
"""
CLI extraction tester — Step 3 & 4 from the refactoring plan.

Usage:
    python test_extract.py <URL>
    python test_extract.py https://www.youtube.com/watch?v=dQw4w9WgXcQ
    python test_extract.py https://www.instagram.com/reel/XXXXXX/

This lets you verify extraction end-to-end WITHOUT starting the server,
which is far faster than debugging through Android.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

# Make sure the extractor package is importable regardless of cwd
import os
sys.path.insert(0, os.path.dirname(__file__))

from extractor import yt_dlp_service
from extractor.errors import ExtractionException


async def main(url: str) -> None:
    request_id = "cli-test-001"

    print(f"\n{'='*60}")
    print(f"  Extraction test")
    print(f"  URL:        {url}")
    print(f"  request_id: {request_id}")
    print(f"{'='*60}\n")

    t0 = time.monotonic()
    try:
        result = await yt_dlp_service.extract(url=url, request_id=request_id)
    except ExtractionException as exc:
        print(f"[FAILED] code={exc.code} retryable={exc.retryable}")
        print(f"  message: {exc.message}")
        return
    except Exception as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return

    elapsed = time.monotonic() - t0

    print(f"[SUCCESS] in {elapsed:.2f}s")
    print(f"  platform:   {result.platform}")
    print(f"  media_type: {result.media_type}")
    print(f"  title:      {result.title!r}")
    print(f"  duration:   {result.duration}s")
    print(f"  thumbnail:  {(result.thumbnail or '')[:80]}")
    print(f"  formats:    {len(result.formats)}")
    print(f"  entries:    {len(result.entries)}")

    if result.formats:
        print("\n  Format list:")
        for f in result.formats:
            has = []
            if f.has_video:
                label = f"{f.height}p" if f.height else "video"
                has.append(label)
            if f.has_audio:
                has.append("audio")
            stream_type = "+".join(has) if has else "unknown"
            print(
                f"    [{f.format_id:>6}] {f.ext or '?':4} "
                f"{stream_type:20} "
                f"direct={f.downloadable_directly} "
                f"size={f.filesize or f.filesize_approx or '?'}"
            )

    if result.entries:
        print(f"\n  Carousel / playlist entries: {len(result.entries)}")
        for i, e in enumerate(result.entries[:5]):
            print(f"    [{i}] type={e.media_type} url={bool(e.url)} thumb={bool(e.thumbnail)}")
        if len(result.entries) > 5:
            print(f"    ... and {len(result.entries) - 5} more")

    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python test_extract.py <URL>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
