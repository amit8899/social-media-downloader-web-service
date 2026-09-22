"""
Normalized data models for extraction results.

These models are the stable public contract between the server and Android.
yt-dlp internal fields must never be exposed directly to the client.
"""
from __future__ import annotations
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel


class MediaType(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    UNKNOWN = "unknown"


class MediaFormat(BaseModel):
    """
    A single downloadable format (stream) returned by yt-dlp.

    has_video + has_audio together describe what the stream contains:
      - has_video=True,  has_audio=True  → combined / progressive stream → downloadable directly
      - has_video=True,  has_audio=False → video-only stream (needs merging for sound)
      - has_video=False, has_audio=True  → audio-only stream
    """
    format_id: str
    ext: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    filesize: Optional[int] = None
    filesize_approx: Optional[int] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    abr: Optional[float] = None      # audio bitrate kbps
    vbr: Optional[float] = None      # video bitrate kbps
    tbr: Optional[float] = None      # total bitrate kbps
    protocol: Optional[str] = None
    url: Optional[str] = None
    audio_url: Optional[str] = None
    has_video: bool = False
    has_audio: bool = False
    downloadable_directly: bool = True  # False when needs server-side merge
    quality_label: Optional[str] = None  # e.g. "1080p", "720p", "audio only"


class CarouselEntry(BaseModel):
    """One item inside an Instagram carousel or playlist."""
    id: Optional[str] = None
    media_type: MediaType = MediaType.UNKNOWN
    url: Optional[str] = None
    thumbnail: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    ext: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[float] = None


class ExtractionError(BaseModel):
    code: str               # e.g. "EXTRACTION_FAILED"
    message: str            # user-safe message
    retryable: bool = False


class ExtractionResult(BaseModel):
    """
    Normalized result returned to Android after a successful or failed extraction.
    Android must never receive raw yt-dlp objects.
    """
    success: bool
    request_id: str

    # Populated on success
    platform: Optional[str] = None
    media_type: Optional[MediaType] = None
    id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    uploader: Optional[str] = None
    thumbnail: Optional[str] = None
    duration: Optional[float] = None
    webpage_url: Optional[str] = None
    formats: List[MediaFormat] = []
    entries: List[CarouselEntry] = []   # for carousels / playlists

    # Populated on failure
    error: Optional[ExtractionError] = None
