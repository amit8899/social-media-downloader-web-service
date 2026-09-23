"""
Normalized error codes and exception types used by the extractor service.

Rule: never expose raw yt-dlp tracebacks or internal error strings to the client.
"""
from __future__ import annotations


class ExtractionException(Exception):
    """Base class for all extractor-layer errors."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# ── Specific error types ────────────────────────────────────────────────────

class InvalidURLError(ExtractionException):
    def __init__(self, detail: str = "The provided URL is not a valid HTTP/HTTPS URL"):
        super().__init__("INVALID_URL", detail, retryable=False)


class UnsupportedURLError(ExtractionException):
    def __init__(self, detail: str = "This URL is not supported by the extraction service"):
        super().__init__("UNSUPPORTED_URL", detail, retryable=False)


class ExtractionFailedError(ExtractionException):
    def __init__(self, detail: str = "Unable to extract media information"):
        super().__init__("EXTRACTION_FAILED", detail, retryable=True)


class VideoUnavailableError(ExtractionException):
    def __init__(self, detail: str = "This video is unavailable or has been removed"):
        super().__init__("VIDEO_UNAVAILABLE", detail, retryable=False)


class LoginRequiredError(ExtractionException):
    def __init__(self, detail: str = "Login or authentication is required to access this content"):
        super().__init__("LOGIN_REQUIRED", detail, retryable=False)


class BotDetectionError(ExtractionException):
    def __init__(self, detail: str = "The platform has blocked this request — bot detection triggered"):
        super().__init__("BOT_DETECTION", detail, retryable=True)


class RateLimitedError(ExtractionException):
    def __init__(self, detail: str = "Rate limited by the platform — try again later"):
        super().__init__("RATE_LIMITED", detail, retryable=True)


class GeoRestrictedError(ExtractionException):
    def __init__(self, detail: str = "This content is not available in the server's region"):
        super().__init__("GEO_RESTRICTED", detail, retryable=False)


class TemporaryPlatformError(ExtractionException):
    def __init__(self, detail: str = "The platform is temporarily unavailable"):
        super().__init__("TEMPORARY_PLATFORM_ERROR", detail, retryable=True)


class ExtractionTimeoutError(ExtractionException):
    def __init__(self, detail: str = "Extraction timed out — the platform may be slow"):
        super().__init__("EXTRACTION_TIMEOUT", detail, retryable=True)


class ServerConfigurationError(ExtractionException):
    def __init__(self, detail: str = "Server configuration error — please contact support"):
        super().__init__("SERVER_CONFIGURATION_ERROR", detail, retryable=False)


# ── Error classifier ────────────────────────────────────────────────────────

def classify_yt_dlp_error(exc: Exception) -> ExtractionException:
    """
    Map a raw yt-dlp exception to a normalized ExtractionException.
    The raw exception message is examined for known patterns but is NOT
    forwarded to the client — only the safe normalized message is.
    """
    import yt_dlp.utils as yt_utils

    raw = str(exc).lower()

    # Order matters — check most specific patterns first

    if "is not a valid url" in raw or "unsupported url" in raw:
        return UnsupportedURLError()

    # Geo check before login — "not available in your country" contains "not available"
    if any(k in raw for k in ("geo", "not available in your country",
                               "region", "territory", "country")):
        return GeoRestrictedError()

    if any(k in raw for k in ("bot", "captcha", "unusual traffic",
                               "po token", "gvs", "botguard", "confirm you",
                               "sign in to confirm", "are you human")):
        return BotDetectionError()

    if any(k in raw for k in ("log in", "login", "authentication",
                               "private", "account is private", "private account",
                               "checkpoint", "cookies", "--cookies")):
        return LoginRequiredError()

    if any(k in raw for k in ("video unavailable", "video is unavailable",
                               "is not available", "no longer available",
                               "removed", "deleted", "404")):
        return VideoUnavailableError()

    if any(k in raw for k in ("rate limit", "too many requests", "429")):
        return RateLimitedError()

    if any(k in raw for k in ("temporarily unavailable", "service unavailable",
                               "503", "502")):
        return TemporaryPlatformError()

    # yt-dlp internal client config error — usually means wrong player client
    # for this IP/context. Retryable because a client switch may fix it.
    if "innertube_context" in raw or "keyerror" in raw:
        return BotDetectionError(
            "YouTube blocked this request — the extraction client needs updating"
        )

    if isinstance(exc, yt_utils.UnsupportedError):
        return UnsupportedURLError()

    if isinstance(exc, yt_utils.DownloadError):
        return ExtractionFailedError("Unable to extract media information from this URL")

    return ExtractionFailedError()
