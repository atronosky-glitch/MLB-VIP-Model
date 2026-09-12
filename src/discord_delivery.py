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


def deliver_arbitrage_alerts(
    opportunities: list[dict[str, Any]],
    webhook_urls: list[str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Push newly-detected arbitrage opportunities to Discord.

    Unlike deliver_recommendations, callers pass exactly the
    opportunities worth alerting (e.g. src/arb_middle_scan.py's
    "new_arbitrage") -- no DB query or threshold filtering happens
    here, since sync_arbitrage_opportunities already decided what's new.
    """
    if not webhook_urls or not opportunities:
        return {"sent": 0, "errors": 0}
    from src.message_formatter import format_arbitrage_alert
    text = format_arbitrage_alert(opportunities)
    return _deliver_chunks(text, webhook_urls, dry_run=dry_run)


def deliver_middle_alerts(
    opportunities: list[dict[str, Any]],
    webhook_urls: list[str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Push newly-detected middle opportunities to Discord. See
    deliver_arbitrage_alerts -- same shape, for middles."""
    if not webhook_urls or not opportunities:
        return {"sent": 0, "errors": 0}
    from src.message_formatter import format_middle_alert
    text = format_middle_alert(opportunities)
    return _deliver_chunks(text, webhook_urls, dry_run=dry_run)


def deliver_new_recommendation_alerts(
    db_path: str | Path,
    webhook_urls: list[str],
    *,
    min_confidence: float = 40.0,
    min_ev_pct: float = 2.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Push EV picks that haven't been alerted to Discord before.

    Unlike deliver_recommendations (the once-daily full summary, which
    resends every currently-actionable rec each time it's called), this
    only sends recommendations never previously alerted -- tracked via
    database.db_manager's discord_alerts_sent table -- so it's safe to
    call on a tight interval (e.g. every arb/middle scan) without
    repeating picks it already pushed. historical_recommendations rows
    are frozen at creation, so this dedup can't just compare against
    "currently active" the way arbitrage/middle opportunities do.
    """
    if not webhook_urls:
        return {"sent": 0, "errors": 0}

    from database.db_manager import (
        get_connection, get_unalerted_recommendation_ids, mark_recommendations_alerted,
    )

    recs = _load_actionable_recommendations(
        db_path, min_confidence=min_confidence, min_ev_pct=min_ev_pct
    )
    if not recs:
        return {"sent": 0, "errors": 0}

    conn = get_connection(str(db_path))
    try:
        ids = [r["recommendation_id"] for r in recs if r.get("recommendation_id")]
        new_ids = set(get_unalerted_recommendation_ids(conn, ids))
        new_recs = [r for r in recs if r.get("recommendation_id") in new_ids]
        if not new_recs:
            return {"sent": 0, "errors": 0}

        from src.message_formatter import format_recommendation
        label = "Pick" if len(new_recs) == 1 else "Picks"
        header = f"**\U0001F514 New EV {label} ({len(new_recs)})**\n\n"
        text = header + "\n\n".join(format_recommendation(r) for r in new_recs)
        result = _deliver_chunks(text, webhook_urls, dry_run=dry_run)

        if not dry_run and result.get("errors", 0) == 0:
            mark_recommendations_alerted(conn, [r["recommendation_id"] for r in new_recs])

        result["recommendation_count"] = len(new_recs)
        return result
    finally:
        conn.close()


def _deliver_chunks(text: str, webhook_urls: list[str], *, dry_run: bool = False) -> dict[str, Any]:
    """Shared chunk-and-send loop for the alert-style delivery functions
    above (deliver_recommendations has its own copy of this pattern;
    left alone here to avoid changing its already-tested behavior)."""
    chunks = chunk_message(text)
    sent = 0
    errors = 0
    for webhook_url in webhook_urls:
        for chunk in chunks:
            if dry_run:
                logger.info("[DRY RUN] Would send to %s: %s", webhook_url[:40], chunk[:80])
                sent += 1
                continue
            if _send_webhook(webhook_url, chunk):
                sent += 1
            else:
                errors += 1
    return {"sent": sent, "errors": errors}


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
    from database.db_manager import get_connection

    conn = get_connection(str(db_path))
    try:
        # Check table exists. r["name"], not r[0] -- production Postgres
        # uses RealDictCursor, whose rows have no positional access (see
        # database/db_manager.py's sync_arbitrage_opportunities history
        # for the same bug class breaking a different feature silently).
        tables = {
            r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "historical_recommendations" not in tables:
            return []

        # rec_status values actually written by src/prop_config.py's
        # classification (see BET_STATUS_*/YN_STATUS_* there) -- 'BET'/
        # 'LEAN' never existed in this schema, so this query previously
        # matched zero rows in production, silently.
        cursor = conn.execute("""
            SELECT *
            FROM historical_recommendations
            WHERE rec_status IN ('STRONG_EDGE', 'POSITIVE_EDGE', 'STRONG_PRICE_OUTLIER', 'PRICE_OUTLIER')
              AND (ev_pct >= ? OR yn_implied_prob_adv >= ? OR ev_pct IS NULL)
            ORDER BY ev_pct DESC
            LIMIT 50
        """, (min_ev_pct, min_ev_pct))

        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
