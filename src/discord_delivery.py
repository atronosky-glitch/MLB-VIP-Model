"""Discord webhook delivery for recommendations.

Sends formatted recommendation messages to Discord channels via webhooks.
Handles rate limiting, message chunking, retry logic, and gracefully
degrades when no webhooks are configured.

Never presents YN price advantage as model EV.
"""

from __future__ import annotations

import json
import logging
import os
import re
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

# Live production testing (2026-09-19) found every real send failing
# with HTTP 403 and a body of "error code: 1010" -- a Cloudflare edge
# block (Cloudflare error 1010: client signature blocked), not a
# Discord API rejection. Python's urllib defaults to a
# "Python-urllib/3.x" User-Agent, which Cloudflare's WAF commonly
# flags as a bot signature and blocks before the request ever reaches
# Discord. A browser-like User-Agent clears it. This is almost
# certainly why zero EV-pick alerts had ever been delivered, despite
# MLB_DISCORD_WEBHOOKS being correctly configured the whole time.
DISCORD_REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Diagnostic-only: the last real HTTP response status(es) observed by
# _send_webhook_raw, for the "simulate" CLI command below to report
# back to an operator (e.g. "Discord actually returned 204"). Never
# consulted by any real delivery-decision logic -- purely observational,
# reset by simulate_delivery() before each attempt it measures.
_last_response_statuses: list[int] = []


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

    2026-09-18: claims each candidate (an atomic, database-level INSERT
    -- see database.db_manager.claim_recommendations_for_alert) BEFORE
    the Discord POST, not after it, so two processes racing to alert the
    same pick can never both send it -- whichever loses the claim just
    skips it. A dry run releases its claim immediately (never leaves a
    pick looking "already alerted" from a run that sent nothing), and a
    real failed send releases its claim too, so the pick is retried on
    the next scan instead of being silently stuck.
    """
    logger.info("[DISCORD] EV webhook configured: %s", "yes" if webhook_urls else "no")
    if not webhook_urls:
        return {"sent": 0, "errors": 0}

    from database.db_manager import (
        get_connection, get_unalerted_recommendation_ids,
        claim_recommendations_for_alert, release_recommendation_alerts,
    )

    recs = _load_actionable_recommendations(
        db_path, min_confidence=min_confidence, min_ev_pct=min_ev_pct
    )
    if not recs:
        return {"sent": 0, "errors": 0}

    conn = get_connection(str(db_path))
    try:
        ids = [r["recommendation_id"] for r in recs if r.get("recommendation_id")]
        candidate_ids = set(get_unalerted_recommendation_ids(conn, ids))
        skipped = len(ids) - len(candidate_ids)
        if skipped:
            logger.info("[DISCORD] Duplicate skipped: %d EV recommendation(s) already alerted", skipped)
        candidates = [r for r in recs if r.get("recommendation_id") in candidate_ids]
        if not candidates:
            return {"sent": 0, "errors": 0}

        candidate_ids_list = [r["recommendation_id"] for r in candidates]
        claimed_ids = set(claim_recommendations_for_alert(conn, candidate_ids_list))
        new_recs = [r for r in candidates if r["recommendation_id"] in claimed_ids]
        if not new_recs:
            # Every candidate lost the claim race to a concurrent run.
            logger.info(
                "[DISCORD] Duplicate skipped: %d EV recommendation(s) claimed by a concurrent run",
                len(candidates),
            )
            return {"sent": 0, "errors": 0}

        for r in new_recs:
            logger.info("[DISCORD] Sending EV recommendation id=%s", r.get("recommendation_id"))

        from src.message_formatter import format_recommendation
        label = "Pick" if len(new_recs) == 1 else "Picks"
        header = f"**\U0001F514 New EV {label} ({len(new_recs)})**\n\n"
        text = header + "\n\n".join(format_recommendation(r) for r in new_recs)
        result = _deliver_chunks(text, webhook_urls, dry_run=dry_run)

        new_ids_list = [r["recommendation_id"] for r in new_recs]
        if dry_run:
            # A dry run must not leave a lasting claim behind.
            release_recommendation_alerts(conn, new_ids_list)
        elif result.get("errors", 0) == 0:
            for rid in new_ids_list:
                logger.info("[DISCORD] EV recommendation id=%s delivered successfully", rid)
        else:
            logger.error(
                "[DISCORD] Delivery failed for %d EV recommendation(s) (ids=%s); will retry next scan",
                len(new_ids_list), new_ids_list,
            )
            release_recommendation_alerts(conn, new_ids_list)

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
                headers=DISCORD_REQUEST_HEADERS,
                method="POST",
            )

            _last_request_time = time.monotonic()

            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.getcode()
                # Discord's real webhook success response is 204 No
                # Content (confirmed against Discord's own docs) -- 200
                # is accepted too in case that ever changes, but 204 is
                # the one that actually matters here and must never be
                # mistaken for a failure.
                if status in (200, 204):
                    logger.info("[DISCORD] Response status=%d", status)
                    _last_response_statuses.append(status)
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

                logger.warning("[DISCORD] Response status=%d (not accepted)", status)
                _last_response_statuses.append(status)
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

            logger.warning("[DISCORD] HTTP error %d sending webhook (attempt %d)", exc.code, attempt + 1)
            _last_response_statuses.append(exc.code)
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


# Matches a Discord webhook URL (any of discord.com/discordapp.com,
# with or without a leading "www.") so it can never end up in a
# returned/logged diagnostic string even if it somehow appears inside
# a response body or exception message.
_WEBHOOK_URL_RE = re.compile(
    r"https://(?:www\.)?discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE,
)


def _redact_webhook_urls(text: str | None) -> str | None:
    if text is None:
        return None
    return _WEBHOOK_URL_RE.sub("[REDACTED_WEBHOOK_URL]", text)


def _send_webhook_diagnostic(webhook_url: str, content: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Single-attempt (no retry, no rate-limit sleep) diagnostic POST to a
    Discord webhook -- returns full diagnostic detail for
    test_mlb_discord_connection() to report back, instead of the plain
    bool send_webhook_message()/_send_webhook_raw() return (those two
    stay unchanged; this is a separate, test-only code path so normal
    delivery's retry/rate-limit behavior is never affected by this).

    Discord's real webhook success response is 204 No Content (200
    accepted too, in case that ever changes) -- same check
    _send_webhook_raw() already uses.

    Never includes the webhook URL itself anywhere in the return value;
    the response body and any exception message are passed through
    _redact_webhook_urls() defensively even though neither is expected
    to contain it."""
    payload = {"content": content}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=data, headers=DISCORD_REQUEST_HEADERS, method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body_text = _redact_webhook_urls(resp.read().decode("utf-8", errors="replace"))
        return {
            "success": status in (200, 204),
            "http_status": status,
            "response_body": body_text,
            "exception_type": None,
            "exception_message": None,
            "timeout": timeout,
        }
    except urllib.error.HTTPError as exc:
        try:
            body_text = _redact_webhook_urls(exc.read().decode("utf-8", errors="replace"))
        except Exception:
            body_text = None
        return {
            "success": False,
            "http_status": exc.code,
            "response_body": body_text,
            "exception_type": type(exc).__name__,
            "exception_message": _redact_webhook_urls(str(exc)),
            "timeout": timeout,
        }
    except Exception as exc:
        return {
            "success": False,
            "http_status": None,
            "response_body": None,
            "exception_type": type(exc).__name__,
            "exception_message": _redact_webhook_urls(str(exc)),
            "timeout": timeout,
        }


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


def _fake_ev_recommendation(rec_id: str) -> dict[str, Any]:
    """A synthetic, fully-qualifying EV pick -- same rec_status tier and
    ev_pct comfortably above the "actionable" bar _load_actionable_
    recommendations enforces, so it exercises the real qualification
    filter rather than bypassing it."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "recommendation_id": rec_id, "fingerprint": f"fp-{rec_id}",
        "event_id": "SIMULATE-E1", "player_id": "SIMULATE-P1", "player_name": "Simulated Test Player",
        "market_type": "strikeouts", "market_form": "ou", "period": "full_game",
        "line": 6.5, "side": "OVER", "sportsbook": "DraftKings",
        "offered_american_odds": -110, "offered_decimal_odds": 1.909, "offered_implied_prob": 0.524,
        "fair_prob": 0.58, "n_consensus_books": 6,
        "ev_pct": 8.0, "rec_status": "STRONG_EDGE", "rec_eligible": 1,
        "scan_timestamp": now, "matchup": "Simulated Away @ Simulated Home",
        "league": "MLB", "sport": "baseball",
    }


def _fake_middle_opportunity() -> dict[str, Any]:
    """A synthetic middle with verdict='WORTH_IT' -- the same gate
    src/worker.py itself applies before ever calling deliver_middle_
    alerts (see _deliver_new_opportunity_alerts)."""
    return {
        "event_id": "SIMULATE-E2", "matchup": "Simulated Away @ Simulated Home",
        "event_start_time": datetime.now(timezone.utc).isoformat(),
        "player_id": "SIMULATE-P2", "player_name": "Simulated Test Batter",
        "market_type": "batting_totalBases_ou", "league": "MLB", "sport": "baseball",
        "over_line": 1.5, "over_sportsbook": "DraftKings", "over_price": -110,
        "over_decimal_odds": 1.909, "over_stake_pct": 0.5,
        "under_line": 2.5, "under_sportsbook": "FanDuel", "under_price": -110,
        "under_decimal_odds": 1.909, "under_stake_pct": 0.5,
        "window_width": 1.0, "worst_case_roi_pct": -2.0, "best_case_roi_pct": 40.0,
        "hit_probability": 0.35, "hit_probability_confidence": "MODERATE",
        "true_ev_pct": 6.5, "verdict": "WORTH_IT", "recommended_stake_units": 1.0,
    }


def simulate_delivery(config: Any = None, db_path: str | Path | None = None) -> dict[str, Any]:
    """Send ONE fake, fully-qualifying EV pick and ONE fake, fully-
    qualifying WORTH_IT middle through the EXACT SAME production
    functions src/worker.py's real scan job calls --
    src.worker._deliver_new_opportunity_alerts itself, not a
    hand-rolled webhook POST -- against whatever Discord webhooks are
    actually configured in THIS environment (the same
    src.production_config.load_config() the real worker process loads
    on startup). Runs the whole thing twice to prove duplicate
    protection actually holds. Uses a throwaway temp SQLite database by
    default, so it can never write fake data into a real
    historical_recommendations/middle_opportunities table -- pass
    db_path to point it at a real database instead (e.g. to prove the
    claim survives a real Postgres round-trip). Never returns or logs a
    webhook URL, only whether each channel is configured.
    """
    import sys
    import tempfile
    import uuid

    from database.db_manager import (
        init_db, get_connection, sync_middle_opportunities,
        get_active_middle_opportunities,
    )
    from src.worker import _deliver_new_opportunity_alerts, _release_undeliverable_claims

    # When this file runs as `python -m src.discord_delivery`, it
    # executes as __main__ -- a SEPARATE module object from
    # `src.discord_delivery` (what src/worker.py actually imports and
    # where the real _send_webhook_raw runs), each with its own
    # _last_response_statuses list. Go through sys.modules explicitly so
    # this always reads the same list worker.py's code path writes to,
    # regardless of how simulate_delivery itself was invoked.
    _dd = sys.modules.get("src.discord_delivery") or __import__(
        "src.discord_delivery", fromlist=["_last_response_statuses"]
    )

    if config is None:
        from src.production_config import load_config
        config = load_config()

    ev_urls = [u.strip() for u in (getattr(config, "discord_webhook_urls", "") or "").split(",") if u.strip()]
    middle_urls = [
        u.strip() for u in (getattr(config, "discord_webhook_urls_middle", "") or "").split(",") if u.strip()
    ]

    owns_db = db_path is None
    if owns_db:
        fd, db_path = tempfile.mkstemp(suffix=".db", prefix="discord_simulate_")
        os.close(fd)
    db_path = str(db_path)

    class _SimConfig:
        """Mirrors src.production_config.ProductionConfig's fields that
        src.worker._deliver_new_opportunity_alerts actually reads --
        real EV/middle webhook URLs from the real config, arbitrage
        left unconfigured since this simulates only what was asked for
        (one EV pick, one middle)."""
        discord_webhook_urls = ",".join(ev_urls)
        discord_webhook_urls_arb_middle = ""
        discord_webhook_urls_middle = ",".join(middle_urls)
        database_path = db_path
        min_confidence_score = 0
        min_ev_pct = 0

    report: dict[str, Any] = {
        "ev_webhook_configured": bool(ev_urls),
        "middle_webhook_configured": bool(middle_urls),
        "db_path": db_path,
        "runs": [],
    }

    try:
        init_db(db_path)
        conn = get_connection(db_path)

        rec_id = f"SIM-EV-{uuid.uuid4().hex[:10]}"
        rec = _fake_ev_recommendation(rec_id)
        cols = ", ".join(rec.keys())
        placeholders = ", ".join("?" * len(rec))
        conn.execute(
            f"INSERT INTO historical_recommendations ({cols}) VALUES ({placeholders})",
            tuple(rec.values()),
        )
        conn.commit()

        def _ev_claimed() -> bool:
            row = conn.execute(
                "SELECT sent_at FROM discord_alerts_sent WHERE alert_type = 'ev_pick' AND alert_key = ?",
                (rec_id,),
            ).fetchone()
            return row is not None

        def _middle_discord_sent() -> bool | None:
            rows = get_active_middle_opportunities(conn, "MLB")
            row = next((r for r in rows if r.get("event_id") == "SIMULATE-E2"), None)
            return bool(row["discord_sent"]) if row else None

        for attempt in (1, 2):
            _dd._last_response_statuses.clear()
            ev_claimed_before = _ev_claimed()

            mid_opps = [_fake_middle_opportunity()]
            sync_result = sync_middle_opportunities(conn, "MLB", mid_opps)
            new_mid_ids = set(sync_result["new_ids"])
            new_middles = [o for o in mid_opps if o.get("opportunity_id") in new_mid_ids]

            results = {"MLB": {
                "league": "MLB", "rows_examined": 0,
                "arbitrage": {"detected": 0},
                "middles": {"detected": 1, **sync_result},
                "new_arbitrage": [],
                "new_middles": new_middles,
            }}
            # Mirrors src.worker._run_arb_middle_scan's exact sequence:
            # release any claim for a channel that has no webhook
            # configured (sync already claimed it unconditionally),
            # THEN attempt delivery for whichever channels do.
            _release_undeliverable_claims(conn, _SimConfig(), results)
            _deliver_new_opportunity_alerts(conn, _SimConfig(), results)

            report["runs"].append({
                "attempt": attempt,
                # True only on the attempt that actually claimed+sent it;
                # both attempts read True for "claimed_in_db" once it's
                # ever succeeded, so this is what distinguishes "sent
                # just now" from "already sent, correctly skipped".
                "ev_sent_this_attempt": (not ev_claimed_before) and _ev_claimed(),
                "ev_claimed_in_db": _ev_claimed(),
                "middle_claimed_as_new_this_attempt": len(new_middles) > 0,
                "middle_discord_sent_in_db": _middle_discord_sent(),
                "response_statuses": list(_dd._last_response_statuses),
            })
    finally:
        if owns_db:
            try:
                os.remove(db_path)
            except OSError:
                pass

    return report


# ── Manual webhook verification (2026-09-15) ──────────────────────────
#
# Confirmed live 2026-09-15: neither Discord channel had ever delivered
# anything, root-caused to MLB_DISCORD_WEBHOOKS/_ARB_MIDDLE/_MIDDLE never
# being declared in render.yaml's worker service. This command lets an
# operator confirm each configured webhook is actually reachable, without
# touching dedup state (discord_alerts_sent) or sending any real alert
# content -- safe to run as often as needed.

def test_webhooks(config: Any = None) -> dict[str, Any]:
    """Send one clearly-labeled test message to every configured Discord
    webhook (EV picks, arbitrage/middle, middles-only), one URL at a
    time, reporting PASS/FAIL per URL. Never touches dedup state, never
    sends real alert content."""
    if config is None:
        from src.production_config import load_config
        config = load_config()

    channels = {
        "ev_picks": getattr(config, "discord_webhook_urls", "") or "",
        "arbitrage": getattr(config, "discord_webhook_urls_arb_middle", "") or "",
        "middles": getattr(config, "discord_webhook_urls_middle", "") or "",
    }

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    results: dict[str, Any] = {}
    any_configured = False

    for channel_name, raw_urls in channels.items():
        urls = [u.strip() for u in raw_urls.split(",") if u.strip()]
        if not urls:
            results[channel_name] = {"configured": False, "urls_tested": 0, "passed": 0, "failed": 0}
            continue
        any_configured = True
        passed = failed = 0
        for url in urls:
            ok = send_webhook_message(
                url,
                f"MLB VIP Model — Discord delivery test ({channel_name}) — {timestamp}. "
                "If you can see this, this channel's webhook is working correctly.",
                embed_title="✅ Delivery Test",
                embed_color=0x00CC66,
            )
            if ok:
                passed += 1
            else:
                failed += 1
        results[channel_name] = {"configured": True, "urls_tested": len(urls), "passed": passed, "failed": failed}

    return {"any_configured": any_configured, "channels": results}


DISCORD_TEST_TIMEOUT_SECONDS = 10.0


def test_mlb_discord_connection(config: Any = None) -> dict[str, Any]:
    """Production-safe connectivity test for MLB_DISCORD_WEBHOOKS
    specifically (2026-09-19 operator request) -- separate from
    test_webhooks() above (which tests all three channels with a
    channel-labeled message) because this one exists to answer one
    narrow, urgent question with no ambiguity: is THIS EXACT env var,
    read the SAME way production reads it, actually reachable right now.
    Sends the literal text "MLB Discord connection test" -- nothing
    else -- to every URL in MLB_DISCORD_WEBHOOKS, via
    _send_webhook_diagnostic() (single attempt, no retry -- a
    diagnostic test needs the RAW first-attempt result, not one
    smoothed over by send_webhook_message()'s own retry loop). Never
    touches dedup state (discord_alerts_sent), never logs or returns
    the webhook URL itself -- only whether it was detected, and for
    each URL: success, HTTP status, response body, exception
    type/message, timeout, and a 1-based webhook index. The response
    body and any exception message are redacted defensively even
    though neither is expected to contain the URL/token."""
    if config is None:
        from src.production_config import load_config
        config = load_config()

    raw = getattr(config, "discord_webhook_urls", "") or ""
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    configured = bool(urls)
    logger.info("[DISCORD] MLB_DISCORD_WEBHOOKS detected: %s", "yes" if configured else "no")
    if not configured:
        return {"configured": False, "urls_tested": 0, "passed": 0, "failed": 0, "attempts": []}

    passed = failed = 0
    attempts: list[dict[str, Any]] = []
    for i, url in enumerate(urls, start=1):
        diag = _send_webhook_diagnostic(url, "MLB Discord connection test", timeout=DISCORD_TEST_TIMEOUT_SECONDS)
        logger.info(
            "[DISCORD] MLB_DISCORD_WEBHOOKS test send %d/%d: success=%s http_status=%s "
            "exception_type=%s exception_message=%s response_body=%s",
            i, len(urls), diag["success"], diag["http_status"],
            diag["exception_type"], diag["exception_message"], diag["response_body"],
        )
        attempts.append({"webhook_index": i, **diag})
        if diag["success"]:
            passed += 1
        else:
            failed += 1
    return {
        "configured": True, "urls_tested": len(urls), "passed": passed, "failed": failed,
        "attempts": attempts,
    }


def replay_most_recent_arbitrage_delivery(config: Any = None) -> dict[str, Any]:
    """One-shot fallback (2026-09-19 operator request): live production
    verification found EV-pick and middle delivery both actually
    succeeding for real opportunities after the Cloudflare/User-Agent
    fix, but zero arbitrage opportunities have EVER been delivered
    (discord_sent=1 in 0 of 935 all-time rows) -- no genuinely new
    arbitrage has occurred to observe naturally. This replays ONLY the
    Discord-delivery portion of the real pipeline for the single most
    recent real, currently-ACTIVE, not-yet-claimed arbitrage
    opportunity already sitting in the database -- never fabricates an
    opportunity. Uses the exact same claim_arbitrage_for_discord /
    deliver_arbitrage_alerts / release_arbitrage_discord_claim
    functions src.worker._deliver_new_opportunity_alerts calls for a
    real scan, so this is a real, correctly-deduplicated send, not a
    simulation: a successful send leaves the claim in place (won't
    re-alert this opportunity while it stays continuously ACTIVE,
    exactly like the real pipeline); a failed send releases the claim
    so the next real scan retries it instead of it being silently
    stuck. Never logs or returns a webhook URL."""
    import sys

    from database.db_manager import (
        get_connection, claim_arbitrage_for_discord, release_arbitrage_discord_claim,
    )

    if config is None:
        from src.production_config import load_config
        config = load_config()

    arb_urls = [
        u.strip() for u in (getattr(config, "discord_webhook_urls_arb_middle", "") or "").split(",") if u.strip()
    ]
    logger.info("[DISCORD] Arbitrage webhook configured: %s", "yes" if arb_urls else "no")
    if not arb_urls:
        return {"configured": False, "replayed": False, "reason": "arbitrage webhook not configured"}

    conn = get_connection(config.database_path)
    try:
        row = conn.execute("""
            SELECT * FROM arbitrage_opportunities
            WHERE status = 'ACTIVE' AND discord_sent = 0
            ORDER BY detected_at DESC LIMIT 1
        """).fetchone()
        if row is None:
            return {"configured": True, "replayed": False, "reason": "no eligible active arbitrage opportunity found"}

        opp = dict(row)
        opp_id = opp["opportunity_id"]

        claimed = claim_arbitrage_for_discord(conn, [opp_id])
        if not claimed:
            return {
                "configured": True, "replayed": False, "opportunity_id": opp_id,
                "reason": "lost the claim race to a concurrent run",
            }

        _dd = sys.modules.get("src.discord_delivery") or __import__(
            "src.discord_delivery", fromlist=["_last_response_statuses"]
        )
        _dd._last_response_statuses.clear()

        result = deliver_arbitrage_alerts([opp], arb_urls)
        response_statuses = list(_dd._last_response_statuses)
        success = result.get("errors", 0) == 0 and result.get("sent", 0) > 0

        if success:
            logger.info("[DISCORD] Arbitrage replay id=%s delivered successfully", opp_id)
        else:
            logger.error("[DISCORD] Arbitrage replay id=%s failed; releasing claim", opp_id)
            release_arbitrage_discord_claim(conn, [opp_id])

        return {
            "configured": True, "replayed": True, "success": success,
            "opportunity_id": opp_id, "league": opp.get("league"), "market_type": opp.get("market_type"),
            "detected_at": opp.get("detected_at"), "response_statuses": response_statuses,
            "claim_persisted": success,
        }
    finally:
        conn.close()


def deliver_daily_results_summary(
    config: Any = None, *, today_start_iso: str | None = None, date_label: str | None = None,
) -> dict[str, Any]:
    """End-of-day summary (2026-09-21 operator request) to the
    dedicated Results Discord channel -- for each category actually
    delivered to Discord (EV picks, arbitrage, middles), today's and
    all-time record + profit in units. *today_start_iso* is the caller-
    supplied UTC cutoff for "today" (src/worker.py computes it from the
    configured local timezone's midnight, once per day) -- an explicit
    parameter rather than computed here so this function stays testable
    without timezone mocking. *date_label* defaults to today's UTC date
    if not given."""
    if config is None:
        from src.production_config import load_config
        config = load_config()

    urls = [u.strip() for u in (getattr(config, "results_webhook_url", "") or "").split(",") if u.strip()]
    logger.info("[DISCORD] Results webhook configured: %s", "yes" if urls else "no")
    if not urls:
        return {"configured": False, "sent": 0, "errors": 0}

    from database.db_manager import (
        get_connection, get_ev_pick_results_summary,
        get_arbitrage_results_summary, get_middle_results_summary,
    )
    from src.message_formatter import format_daily_results_summary

    conn = get_connection(config.database_path)
    try:
        ev_today = get_ev_pick_results_summary(conn, since=today_start_iso)
        ev_all_time = get_ev_pick_results_summary(conn)
        arb_today = get_arbitrage_results_summary(conn, since=today_start_iso)
        arb_all_time = get_arbitrage_results_summary(conn)
        mid_today = get_middle_results_summary(conn, since=today_start_iso)
        mid_all_time = get_middle_results_summary(conn)
    finally:
        conn.close()

    label = date_label or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = format_daily_results_summary(
        ev_today, ev_all_time, arb_today, arb_all_time, mid_today, mid_all_time, date_label=label,
    )
    result = _deliver_chunks(text, urls)
    return {"configured": True, **result}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m src.discord_delivery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "test-webhooks",
        help="Send a safe, clearly-labeled test message to every configured Discord webhook",
    )
    subparsers.add_parser(
        "test-mlb",
        help='Send the literal message "MLB Discord connection test" to MLB_DISCORD_WEBHOOKS specifically',
    )
    subparsers.add_parser(
        "replay-arbitrage",
        help=(
            "Replay ONLY the Discord-delivery portion of the real pipeline for the single "
            "most recent real, active, not-yet-claimed arbitrage opportunity already in the "
            "database. Never fabricates data; preserves real dedup afterward."
        ),
    )
    subparsers.add_parser(
        "daily-results-summary",
        help=(
            "Send the end-of-day results summary (today's and all-time record + profit in "
            "units, per category) to the Results Discord channel right now."
        ),
    )
    simulate_p = subparsers.add_parser(
        "simulate",
        help=(
            "Send one fake qualifying EV pick and one fake qualifying middle through the "
            "EXACT SAME production delivery path src/worker.py uses, against whatever "
            "webhooks are actually configured in this environment. Runs twice to prove "
            "duplicate protection. Never prints a webhook URL."
        ),
    )
    simulate_p.add_argument(
        "--db-path", default=None,
        help="Use this database instead of a throwaway temp SQLite file (e.g. a real Postgres URL).",
    )
    simulate_p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "simulate":
        report = simulate_delivery(db_path=args.db_path)
        if args.json:
            print(json.dumps(report, indent=2, default=str))
            return 0

        print(f"EV webhook configured:     {'yes' if report['ev_webhook_configured'] else 'no'}")
        print(f"Middle webhook configured: {'yes' if report['middle_webhook_configured'] else 'no'}")
        print()
        for run in report["runs"]:
            print(f"--- Attempt {run['attempt']} ---")
            print(f"  EV sent this attempt:        {run['ev_sent_this_attempt']}")
            print(f"  EV claimed in DB (any time): {run['ev_claimed_in_db']}")
            print(f"  Middle claimed as new:       {run['middle_claimed_as_new_this_attempt']}")
            print(f"  Middle discord_sent in DB:   {run['middle_discord_sent_in_db']}")
            no_status_reason = "not configured" if not (
                report["ev_webhook_configured"] or report["middle_webhook_configured"]
            ) else "no request made -- nothing new to send"
            print(f"  Discord response status(es): {run['response_statuses'] or f'(none -- {no_status_reason})'}")
            print()

        run1, run2 = report["runs"][0], report["runs"][1]
        dedup_ok = (not run2["ev_sent_this_attempt"]) and (not run2["middle_claimed_as_new_this_attempt"])
        print(f"Duplicate prevention on second run: {'PASS' if dedup_ok else 'FAIL'}")
        return 0

    if args.command == "test-mlb":
        result = test_mlb_discord_connection()
        if not result["configured"]:
            print("MLB_DISCORD_WEBHOOKS is not set (empty) in this environment.")
            return 1
        status = "PASS" if result["failed"] == 0 else "FAIL"
        print(f"MLB_DISCORD_WEBHOOKS: {status} ({result['passed']}/{result['urls_tested']} webhook(s) succeeded)")
        for attempt in result["attempts"]:
            print(
                f"  webhook #{attempt['webhook_index']}: success={attempt['success']} "
                f"http_status={attempt['http_status']} exception_type={attempt['exception_type']} "
                f"exception_message={attempt['exception_message']} timeout={attempt['timeout']}"
            )
            print(f"    response_body={attempt['response_body']!r}")
        return 0 if result["failed"] == 0 else 1

    if args.command == "replay-arbitrage":
        result = replay_most_recent_arbitrage_delivery()
        print(json.dumps(result, indent=2, default=str))
        if not result["configured"]:
            return 1
        if not result["replayed"]:
            return 0
        return 0 if result["success"] else 1

    if args.command == "daily-results-summary":
        result = deliver_daily_results_summary()
        print(json.dumps(result, indent=2, default=str))
        if not result["configured"]:
            return 1
        return 0 if result.get("errors", 0) == 0 else 1

    if args.command == "test-webhooks":
        result = test_webhooks()
        if not result["any_configured"]:
            print(
                "No Discord webhooks configured -- MLB_DISCORD_WEBHOOKS, "
                "MLB_DISCORD_WEBHOOKS_ARB_MIDDLE, and MLB_DISCORD_WEBHOOKS_MIDDLE are all empty."
            )
            return 1
        overall_ok = True
        for channel, info in result["channels"].items():
            if not info["configured"]:
                print(f"{channel}: NOT CONFIGURED")
                continue
            status = "PASS" if info["failed"] == 0 else "FAIL"
            if info["failed"]:
                overall_ok = False
            print(f"{channel}: {status} ({info['passed']}/{info['urls_tested']} webhook(s) succeeded)")
        return 0 if overall_ok else 1

    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
