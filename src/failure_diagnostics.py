"""Safe, structured failure details for pipeline/worker logs.

Purpose (2026-09-23): `morning-run-nfl` intermittently exited with code 3
(API failure) but the only trace was a raw ``str(exc)`` on stderr -- which
for a ``requests.HTTPError`` embeds the full request URL, and for The Odds
API that URL carries the API key as a query parameter. This module builds
one small dict that answers "which provider, which stage, which sport, what
HTTP status, what exception class, what (sanitized) message, and did a
retry happen" without ever containing a credential or a full URL with a
query string.

Pure: no I/O, no network.
"""

from __future__ import annotations

import re
from typing import Any

_MAX_MESSAGE_CHARS = 300

# key=value / key: value pairs whose key names a secret
_SECRET_PAIR = re.compile(
    r"(?i)\b(api[_-]?key|apikey|x-api-key|access[_-]?token|token|secret|signature|password|authorization|key)"
    r"\s*([=:])\s*(?:Bearer\s+)?[^\s&,;'\")]+"
)
_URL = re.compile(r"https?://[^\s'\")\]>]+")
# long opaque strings that look like keys/tokens (32+ url-safe chars)
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def _strip_url(match: re.Match) -> str:
    url = match.group(0)
    # keep scheme://host/path, drop the query string and fragment entirely
    return re.split(r"[?#]", url, maxsplit=1)[0]


def sanitize_message(text: Any, max_len: int = _MAX_MESSAGE_CHARS) -> str:
    """Remove credentials and URL query strings from *text* and bound its
    length. Never raises."""
    try:
        s = str(text)
    except Exception:
        return "<unprintable>"
    s = _URL.sub(_strip_url, s)
    s = _SECRET_PAIR.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", s)
    s = _LONG_TOKEN.sub("<redacted>", s)
    s = " ".join(s.split())
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def http_status_of(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def build_failure_detail(
    exc: BaseException, *, provider: str, stage: str, sport: str | None,
    retried: bool | None = None, attempts: int | None = None,
) -> dict:
    """*retried*: True if the client retried before giving up, False if it
    did not, None if unknown. *attempts*: total HTTP attempts when known."""
    return {
        "provider": provider,
        "stage": stage,
        "sport": sport,
        "http_status": http_status_of(exc),
        "exception_class": type(exc).__name__,
        "message": sanitize_message(exc),
        "retried": retried,
        "attempts": attempts,
    }


def format_failure(detail: dict) -> str:
    """One greppable log line, e.g.
    ``provider=sportsgameodds stage=fetch_events sport=football http_status=429 ...``"""
    ordered = ("provider", "stage", "sport", "http_status", "exception_class", "retried", "attempts", "message")
    return " ".join(f"{k}={detail.get(k)!r}" if k == "message" else f"{k}={detail.get(k)}" for k in ordered)
