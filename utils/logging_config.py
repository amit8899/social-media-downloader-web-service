"""
Structured logging utilities.

Rules:
  - Never log cookies, Authorization headers, or credentials
  - Never log complete URLs containing signed tokens
  - Use request_id in every log line to correlate Android/Render debug sessions
"""
from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO") -> None:
    """Configure root logger with a structured format."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    logging.basicConfig(
        stream=sys.stdout,
        level=numeric_level,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Quieten noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
