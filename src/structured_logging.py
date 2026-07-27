"""Structured logging configuration for production.

Provides JSON and human-readable log formatters, job-scoped context
injection, and a setup function wired into the pipeline.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class JSONFormatter(logging.Formatter):
    """Emits each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "job_id"):
            entry["job_id"] = record.job_id  # type: ignore[attr-defined]
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class HumanFormatter(logging.Formatter):
    """Compact human-readable format for terminal output."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


class JobContextFilter(logging.Filter):
    """Injects job_id into every log record from the active job."""

    def __init__(self) -> None:
        super().__init__()
        self.job_id: str | None = None

    def filter(self, record: logging.LogRecord) -> bool:
        if self.job_id is not None:
            record.job_id = self.job_id  # type: ignore[attr-defined]
        return True


_job_filter = JobContextFilter()


def set_job_context(job_id: str | None) -> None:
    """Set or clear the job context for all log records."""
    _job_filter.job_id = job_id


def setup_logging(level: str = "INFO", fmt: str = "human") -> None:
    """Configure root logger for the application.

    Parameters
    ----------
    level:
        Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    fmt:
        ``"human"`` for terminal-friendly output, ``"json"`` for machine-readable.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates on re-setup
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(HumanFormatter())
    handler.addFilter(_job_filter)
    root.addHandler(handler)
