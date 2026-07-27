"""Discord webhook delivery for recommendations.

Sends formatted recommendation messages to Discord channels via webhooks.
Handles rate limiting, message chunking, retry logic, and gracefully
degrades when no webhooks are configured.

Never presents YN price advantage as model EV.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.message_formatter import format_daily_summary, chunk_message, DISCORD_CHAR_LIMIT

logger = logging.getLogger(__name__)

# ── Rate limiting ──────────────────────────────────────────────────

_last_request_time: float = 0.0
MIN_REQUEST_INTERVAL = 1.0  # seconds between webhook calls

# ── Retry settings ─────────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAY_BASE = 2.0  # exponential backoff base


def deliver_recommendations(
    db_path: str | Path,
    webhook_urls: list[str],
    *,
    min_confidence: float = 40.0,
    min_ev_pct: float = 2.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Deliver current recommendations to Discord webhooks.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.
    webhook_urls:
        List of Discord webhook URLs.
    min_confidence:
        Minimum confidence score to include.
    min_ev_pct:
        Minimum EV% or price advantage in percentage points to include.
    dry_run:
        If True, format messages but don't send.

    Returns
    -------
    Dict with delivery stats.
    """
    if not webhook_urls:
        logger.info("No Discord webhooks configured, skipping delivery")
        return {"sent": 0, "skipped": 0, "errors": 0}

    recs = _load_actionable_recommendations(
        db_path, min_confidence=min_confidence, min_ev_pct=min_ev_pct
    )

    if not recs:
        logger.info("No actionable recommendations to deliver")
        return {"sent": 0, "skipped": 0, "errors": 0}

    date_label = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = format_daily_summary(recs, date_label=date_label)
    chunks = chunk_message(text)

    sent = 0
    errors = 0

    for webhook_url in webhook_urls:
        for chunk in chunks:
            if dry_run:
                logger.info("[DRY RUN] Would send to %s: %s", webhook_url[:40], chunk[:80])
                sent += 1
                continue

            success = _send_webhook(webhook_url, chunk)
            if success:
                sent += 1
            else:
                errors += 1

    return {
        "sent": sent,
        "skipped": 0,
        "errors": errors,
        "recommendation_count": len(recs),
    }


def send_webhook_message(
    webhook_url: str,
    content: str,
    *,
    embed_title: str | None = None,
    embed_color: int = 0x00FF00,
) -> bool:
    """Send a single message to a Discord webhook.

    Parameters
    ----------
    webhook_url:
        The Discord webhook URL.
    content:
        Message text.
    embed_title:
        Optional embed title.
    embed_color:
        Embed color as integer.

    Returns
    -------
    True if sent successfully.
    """
    payload: dict[str, Any] = {}

    if embed_title:
        payload["embeds"] = [{
            "title": embed_title,
            "description": content,
            "color": embed_color,
        }]
    else:
        payload["content"] = content

    return _send_webhook_raw(webhook_url, payload)


def _send_webhook(webhook_url: str, content: str) -> bool:
    """Send text content to a Discord webhook with retry."""
    payload = {"content": content}
    return _send_webhook_raw(webhook_url, payload)


def _send_webhook_raw(webhook_url: str, payload: dict[str, Any]) -> bool:
    """Send a JSON payload to a Discord webhook with retry and rate limiting."""
    global _last_request_time

    for attempt in range(MAX_RETRIES):
        # Rate limiting
        elapsed = time.monotonic() - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            _last_request_time = time.monotonic()

            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.getcode()
                if status in (200, 204):
                    return True
                # Rate limited (429)
                if status == 429:
                    retry_after = 2.0
                    try:
                        body = json.loads(resp.read())
                        retry_after = body.get("retry_after", 2.0)
                    except Exception:
                        pass
                    logger.warning("Rate limited, retrying after %.1fs", retry_after)
                    time.sleep(retry_after)
                    continue

                logger.warning("Webhook returned status %d", status)
                return False

        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = RETRY_DELAY_BASE ** (attempt + 1)
                try:
                    body = json.loads(exc.read())
                    retry_after = body.get("retry_after", retry_after)
                except Exception:
                    pass
                logger.warning("Rate limited (429), retry after %.1fs", retry_after)
                time.sleep(retry_after)
                continue

            logger.warning("HTTP error %d sending webhook (attempt %d)", exc.code, attempt + 1)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_BASE ** (attempt + 1))

        except urllib.error.URLError as exc:
            logger.warning("URL error sending webhook: %s (attempt %d)", exc.reason, attempt + 1)
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_BASE ** (attempt + 1))

        except Exception as exc:
            logger.warning("Unexpected error sending webhook: %s", exc)
            return False

    logger.error("Failed to send webhook after %d attempts", MAX_RETRIES)
    return False


def _load_actionable_recommendations(
    db_path: str | Path,
    *,
    min_confidence: float = 40.0,
    min_ev_pct: float = 2.0,
) -> list[dict[str, Any]]:
    """Load actionable recommendations from DB."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Check table exists
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "historical_recommendations" not in tables:
            return []

        cursor = conn.execute("""
            SELECT *
            FROM historical_recommendations
            WHERE rec_status IN ('BET', 'LEAN')
              AND (ev_pct >= ? OR yn_implied_prob_adv >= ? OR ev_pct IS NULL)
            ORDER BY ev_pct DESC
            LIMIT 50
        """, (min_ev_pct, min_ev_pct))

        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
