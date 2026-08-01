"""Daily production pipeline for MLB sportsbook analysis.

Runs the complete workflow from configuration validation through
report generation in a single command::

    python -m src.daily_pipeline
    python -m src.daily_pipeline --live
    python -m src.daily_pipeline --dry-run
    python -m src.daily_pipeline --market strikeouts --actionable-only

Stages:
    1. Validate configuration
    2. Create pipeline run
    3. Fetch events
    4. Ingest
    5. Validate
    6. Scan
    7. Freeze recommendations
    8. Produce reports
    9. Print terminal summary
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.api_client import SportsGameOddsClient
from src.player_prop_scanner import run_scan
from src.odds_parser import parse_odds
from src.market_analysis import american_to_probability, probability_to_american
from src import prop_config as cfg
from src.prop_config import validate_config, MARKET_REGISTRY
from database.db_manager import (
    DB_PATH, get_connection, create_run, finish_run, log_ingestion,
    save_game, save_raw_response, save_odds_batch, record_pull,
    save_recommendation, persist_scan_error, capture_closing_prices,
)

logger = logging.getLogger(__name__)

# ==================================================================
# Exit codes
# ==================================================================

EXIT_SUCCESS = 0
EXIT_SUCCESS_NO_RECS = 1
EXIT_CONFIG_FAILURE = 2
EXIT_API_FAILURE = 3
EXIT_DB_FAILURE = 4
EXIT_VALIDATION_FAILURE = 5
EXIT_UNEXPECTED_FAILURE = 6


# ==================================================================
# Live-game filtering helpers
# ==================================================================

_LIVE_STATES = {"live", "inprogress", "in_progress", "started", "in-progress"}
_COMPLETED_STATES = {"final", "finished", "completed", "closed", "ended"}


def _is_game_skippable(
    event_status: str,
    start_time: str,
    *,
    now_utc: datetime | None = None,
) -> tuple[bool, str]:
    """Check whether a game should be skipped (not scanned for recommendations).

    Returns (should_skip, reason).  Uses both the explicit event status
    field and the scheduled start time as safety checks.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    status_lower = (event_status or "").lower().strip()

    # 1. Explicit status check
    if status_lower in _LIVE_STATES:
        return True, f"Game is live (status={event_status})"
    if status_lower in _COMPLETED_STATES:
        return True, f"Game is completed (status={event_status})"

    # 2. Start-time check (skip if game has already started)
    if start_time:
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            if now_utc >= start_dt:
                return True, f"Game already started (start={start_time})"
        except (ValueError, TypeError):
            pass  # unparseable time — don't skip on time alone

    return False, ""


def _build_matchup(away_team: str, home_team: str) -> str:
    """Build a human-readable matchup string like 'NYY @ BOS'."""
    away = (away_team or "").strip()
    home = (home_team or "").strip()
    if away and home:
        return f"{away} @ {home}"
    if home:
        return home
    if away:
        return away
    return ""


# ==================================================================
# Pipeline state
# ==================================================================

@dataclass
class PipelineConfig:
    """All configurable pipeline parameters."""
    live: bool = False
    use_cache: bool = False
    auto: bool = False
    output_dir: str = "output"
    market: str = "all"
    market_form: str = "all"
    actionable_only: bool = True
    positive_only: bool = False
    require_fresh: bool = False
    dry_run: bool = False
    as_json: bool = False
    as_csv: bool = False
    debug: bool = False


@dataclass
class PipelineState:
    """Mutable state accumulated across pipeline stages."""
    pipeline_run_id: str = ""
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    version: str = "1.0.0"
    config_summary: dict = field(default_factory=dict)
    execution_mode: str = "cache"
    n_events: int = 0
    n_markets: int = 0
    n_books: int = 0
    n_approved_rows: int = 0
    n_excluded_rows: int = 0
    n_ou_opportunities: int = 0
    n_yn_opportunities: int = 0
    n_recommendations_saved: int = 0
    n_errors: int = 0
    n_warnings: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scan_result: dict = field(default_factory=dict)
    ingestion_run_id: str = ""
    scan_run_id: str = ""
    data_source: str = ""
    stale_warning: bool = False
    research_only: bool = False
    stage_timings: dict = field(default_factory=dict)
    status: str = "RUNNING"
    stage_errors: dict = field(default_factory=dict)
    skipped_games: list[dict] = field(default_factory=list)
    n_games_analyzed: int = 0
    n_games_skipped: int = 0
    n_total_games: int = 0
    has_live_game_recs: bool = False
    n_official_picks: int = 0


# ==================================================================
# Stage 1: Validate configuration
# ==================================================================

def _stage_validate_config(config: PipelineConfig, state: PipelineState) -> bool:
    """Validate all configuration. Returns True if valid."""
    print("[1/9] Validating configuration")
    t0 = time.monotonic()

    errors: list[str] = []

    # API key
    api_key = os.environ.get("SPORTSODDS_API_KEY", "")
    if not api_key and not config.dry_run:
        errors.append("SPORTSODDS_API_KEY not set in environment")

    # Database writability
    if not config.dry_run:
        try:
            db_path = Path(str(DB_PATH))
            if db_path.exists() and not os.access(db_path, os.W_OK):
                errors.append(f"Database not writable: {db_path}")
            elif not db_path.exists():
                parent = db_path.parent
                if parent.exists() and not os.access(parent, os.W_OK):
                    errors.append(f"Database parent directory not writable: {parent}")
        except Exception as exc:
            errors.append(f"Database check failed: {exc}")

    # Cache directory writability
    cache_dir = Path("data/_api_cache")
    if not config.dry_run:
        try:
            if cache_dir.exists() and not os.access(cache_dir, os.W_OK):
                errors.append(f"Cache directory not writable: {cache_dir}")
        except Exception as exc:
            errors.append(f"Cache directory check failed: {exc}")

    # Registry integrity
    try:
        config_errors = validate_config()
        errors.extend(config_errors)
        if not MARKET_REGISTRY:
            errors.append("Market registry is empty")
    except Exception as exc:
        errors.append(f"Registry validation failed: {exc}")

    # Freshness threshold
    try:
        if FRESHNESS_THRESHOLD_SECONDS <= 0:
            errors.append("FRESHNESS_THRESHOLD_SECONDS must be positive")
    except Exception:
        pass

    state.stage_timings["validate_config"] = round(time.monotonic() - t0, 3)

    if errors:
        state.errors.extend(errors)
        state.status = "CONFIG_FAILURE"
        for e in errors:
            print(f"  FATAL: {e}", file=sys.stderr)
        return False

    state.config_summary = {
        "market": config.market,
        "market_form": config.market_form,
        "mode": ("live" if config.live else "cache"),
        "dry_run": config.dry_run,
        "require_fresh": config.require_fresh,
        "output_dir": config.output_dir,
        "actionable_only": config.actionable_only,
        "positive_only": config.positive_only,
    }
    print("  OK — all checks passed")
    return True


# ==================================================================
# Stage 2: Create pipeline run
# ==================================================================

def _stage_create_run(config: PipelineConfig, state: PipelineState) -> bool:
    """Create pipeline run record. Returns True on success."""
    print("[2/9] Creating pipeline run")
    t0 = time.monotonic()

    state.pipeline_run_id = str(uuid.uuid4())
    state.execution_mode = "live" if config.live else "cache"

    if config.dry_run:
        print(f"  DRY RUN — run_id: {state.pipeline_run_id[:8]}... (not persisted)")
        state.stage_timings["create_run"] = round(time.monotonic() - t0, 3)
        return True

    try:
        conn = get_connection()
        try:
            run_id = create_run(
                conn,
                run_type="pipeline",
                mode=state.execution_mode,
                market_filter=config.market,
                form_filter=config.market_form,
                metadata=json.dumps({
                    "pipeline_run_id": state.pipeline_run_id,
                    "version": state.version,
                    "config": state.config_summary,
                }),
            )
            state.pipeline_run_id = run_id
            print(f"  Run created: {run_id[:8]}...")
        finally:
            conn.close()
    except Exception as exc:
        state.errors.append(f"Failed to create run: {exc}")
        state.n_errors += 1
        logger.warning("Could not create pipeline run record: %s", exc)
        print(f"  WARNING: Could not persist run record: {exc}")

    state.stage_timings["create_run"] = round(time.monotonic() - t0, 3)
    return True


# ==================================================================
# Stage 3: Fetch events
# ==================================================================

def _stage_fetch_events(config: PipelineConfig, state: PipelineState) -> bool:
    """Fetch MLB events from API or cache. Returns True on success."""
    print("[3/9] Fetching events")
    t0 = time.monotonic()

    try:
        # Live runs must never analyze a previous day's slate, so bound the
        # cache TTL; research runs may reuse a recent snapshot (1 hour).
        max_cache_age = cfg.LIVE_CACHE_TTL_SECONDS if config.live else 3600.0
        client = SportsGameOddsClient(max_cache_age=max_cache_age)
        data, from_cache = client.get_events(
            league="MLB", odds_available=True, include_alt_lines=True,
        )
        state.data_source = "CACHE" if from_cache else "LIVE API"

        events = data.get("data", data.get("events", [])) or []
        state.n_events = len(events)

        # Count unique sportsbooks across all events
        books: set[str] = set()
        for ev in events:
            odds = ev.get("odds", {}) or {}
            for odd_id, odd_data in odds.items():
                bb = odd_data.get("byBookmaker", {}) or {}
                books.update(bb.keys())
        state.n_books = len(books)

        # Store events for downstream stages
        state.scan_result["_raw_events"] = events

        print(f"  Events: {state.n_events}  |  Sportsbooks: {state.n_books}")
        print(f"  Source: {state.data_source}")

        if not events:
            state.warnings.append("No MLB events found")
            state.n_warnings += 1
            print("  WARNING: No events available")

    except Exception as exc:
        state.errors.append(f"API fetch failed: {exc}")
        state.n_errors += 1
        state.status = "API_FAILURE"
        print(f"  ERROR: {exc}", file=sys.stderr)
        state.stage_timings["fetch_events"] = round(time.monotonic() - t0, 3)
        return False

    state.stage_timings["fetch_events"] = round(time.monotonic() - t0, 3)
    return True


# ==================================================================
# Stage 4: Ingest
# ==================================================================

def _stage_ingest(config: PipelineConfig, state: PipelineState) -> bool:
    """Run ingestion pipeline. Returns True on success."""
    print("[4/9] Ingesting odds")
    t0 = time.monotonic()

    events = state.scan_result.get("_raw_events", [])
    if not events:
        print("  SKIP — no events to ingest")
        state.stage_timings["ingest"] = round(time.monotonic() - t0, 3)
        return True

    if config.dry_run:
        # Count rows without writing
        total_odds = 0
        total_audit = 0
        for event in events:
            parsed = parse_odds(event)
            total_odds += len(parsed.odds_rows)
            total_audit += len(parsed.audit_rows)
        state.n_approved_rows = total_odds
        print(f"  DRY RUN — would ingest {total_odds} odds rows, {total_audit} audit rows")
        state.stage_timings["ingest"] = round(time.monotonic() - t0, 3)
        return True

    try:
        conn = get_connection()
        try:
            ingestion_run_id = create_run(
                conn, run_type="ingestion", mode="full",
                market_filter=config.market, form_filter=config.market_form,
            )
            state.ingestion_run_id = ingestion_run_id

            save_raw_response(conn, "/events", {"league": "MLB"}, {"data": events})

            n_odds_total = 0
            n_audit_total = 0
            n_errors = 0

            for event in events:
                event_id = event.get("eventID") or event.get("id", "")
                try:
                    teams = event.get("teams", {}) or {}
                    home = teams.get("home", {}) or {}
                    away = teams.get("away", {}) or {}
                    status_obj = event.get("status", {}) or {}
                    game_record = {
                        "event_id": event_id,
                        "league": "MLB",
                        "away_team": away.get("names", {}).get("long") or away.get("name", ""),
                        "home_team": home.get("names", {}).get("long") or home.get("name", ""),
                        "start_time": (status_obj.get("startsAt") if isinstance(status_obj, dict)
                                       else event.get("startDate", "")),
                        "status": _parse_status(status_obj),
                        "sport_id": event.get("sportID", ""),
                        "league_id": event.get("leagueID", ""),
                    }
                    save_game(conn, game_record)

                    parsed = parse_odds(event)
                    n_saved = save_odds_batch(conn, parsed.odds_rows, parsed.audit_rows)
                    log_ingestion(conn, ingestion_run_id, event_id,
                                  odds_rows=n_saved, audit_rows=len(parsed.audit_rows))
                    record_pull(conn, event_id, "pipeline")

                    n_odds_total += n_saved
                    n_audit_total += len(parsed.audit_rows)
                except Exception as exc:
                    n_errors += 1
                    logger.warning("Ingestion error for event %s: %s", event_id, exc)
                    log_ingestion(conn, ingestion_run_id, event_id,
                                  error_message=str(exc))

            finish_run(conn, ingestion_run_id,
                       n_events=len(events),
                       n_markets=n_odds_total,
                       data_source=state.data_source)

            state.n_approved_rows = n_odds_total
            state.n_excluded_rows = n_audit_total
            state.n_errors += n_errors

            print(f"  Ingested: {n_odds_total} odds rows, {n_audit_total} audit rows")
            if n_errors:
                state.warnings.append(f"{n_errors} events had ingestion errors")
                state.n_warnings += 1
                print(f"  WARNING: {n_errors} events had errors")

        finally:
            conn.close()

    except Exception as exc:
        state.errors.append(f"Ingestion failed: {exc}")
        state.n_errors += 1
        state.status = "DB_FAILURE"
        print(f"  ERROR: {exc}", file=sys.stderr)
        state.stage_timings["ingest"] = round(time.monotonic() - t0, 3)
        return False

    state.stage_timings["ingest"] = round(time.monotonic() - t0, 3)
    return True


# ==================================================================
# Stage 5: Validate
# ==================================================================

def _stage_validate(config: PipelineConfig, state: PipelineState) -> bool:
    """Validate ingested data. Returns True if valid enough to continue."""
    print("[5/9] Validating data")
    t0 = time.monotonic()

    if state.n_approved_rows == 0:
        state.warnings.append("No approved odds rows after ingestion")
        state.n_warnings += 1
        print("  WARNING: No approved odds rows found")

    # Check freshness
    scan_result = state.scan_result
    if scan_result.get("stale_warning"):
        state.warnings.append("Data is stale (exceeds freshness threshold)")
        state.n_warnings += 1
        state.stale_warning = True
        print(f"  WARNING: Data older than freshness threshold")

    if config.require_fresh and state.stale_warning:
        state.errors.append("Data is stale and --require-fresh is set")
        state.n_errors += 1
        state.status = "VALIDATION_FAILURE"
        print("  FATAL: Stale data with --require-fresh", file=sys.stderr)
        state.stage_timings["validate"] = round(time.monotonic() - t0, 3)
        return False

    state.stage_timings["validate"] = round(time.monotonic() - t0, 3)
    print(f"  Approved rows: {state.n_approved_rows}")
    print(f"  Excluded rows: {state.n_excluded_rows}")
    print("  OK")
    return True


# ==================================================================
# Stage 6: Scan
# ==================================================================

def _stage_scan(config: PipelineConfig, state: PipelineState) -> bool:
    """Run the generic scanner. Returns True on success."""
    print("[6/9] Scanning markets")
    t0 = time.monotonic()

    mode = "all"
    if config.actionable_only:
        mode = "actionable"
    elif config.positive_only:
        mode = "positive"

    try:
        scan_result = run_scan(
            mode=mode,
            market=config.market,
            market_form=config.market_form,
            limit=25,
        )

        state.scan_result = scan_result
        state.n_ou_opportunities = len(scan_result.get("opportunities", []))
        state.n_yn_opportunities = len(scan_result.get("yn_opportunities", []))
        state.n_markets = scan_result.get("n_markets", 0)
        state.stale_warning = scan_result.get("stale_warning", False)
        state.research_only = scan_result.get("research_only", False)
        state.scan_run_id = scan_result.get("run_id", "")

        total_opps = state.n_ou_opportunities + state.n_yn_opportunities
        print(f"  O/U opportunities: {state.n_ou_opportunities}")
        print(f"  YN opportunities:  {state.n_yn_opportunities}")
        print(f"  Total: {total_opps}")

        if total_opps == 0:
            state.warnings.append("No opportunities found")
            state.n_warnings += 1
            print("  No qualifying opportunities found")

    except Exception as exc:
        state.errors.append(f"Scan failed: {exc}")
        state.n_errors += 1
        print(f"  ERROR: {exc}", file=sys.stderr)
        state.stage_timings["scan"] = round(time.monotonic() - t0, 3)
        return False

    state.stage_timings["scan"] = round(time.monotonic() - t0, 3)
    return True


# ==================================================================
# Stage 7: Freeze recommendations
# ==================================================================

def _stage_freeze(config: PipelineConfig, state: PipelineState) -> bool:
    """Persist recommendation snapshots. Returns True on success."""
    print("[7/9] Freezing recommendations")
    t0 = time.monotonic()

    ou_opps = state.scan_result.get("opportunities", [])
    yn_opps = state.scan_result.get("yn_opportunities", [])
    all_opps = ou_opps + yn_opps

    if not all_opps:
        print("  SKIP — no opportunities to freeze")
        state.stage_timings["freeze"] = round(time.monotonic() - t0, 3)
        return True

    if config.dry_run:
        print(f"  DRY RUN — would freeze {len(all_opps)} recommendations")
        state.n_recommendations_saved = len(all_opps)
        state.stage_timings["freeze"] = round(time.monotonic() - t0, 3)
        return True

    try:
        conn = get_connection()
        try:
            # Look up game status for all events in this scan
            event_ids = list({opp["event_id"] for opp in all_opps if opp.get("event_id")})
            game_info: dict[str, dict] = {}
            if event_ids:
                placeholders = ",".join("?" * len(event_ids))
                rows = conn.execute(
                    f"SELECT event_id, away_team, home_team, start_time, status "
                    f"FROM games WHERE event_id IN ({placeholders})",
                    event_ids,
                ).fetchall()
                for row in rows:
                    game_info[row["event_id"]] = {
                        "away_team": row["away_team"] or "",
                        "home_team": row["home_team"] or "",
                        "start_time": row["start_time"] or "",
                        "status": row["status"] or "scheduled",
                    }

            state.n_total_games = len(game_info)

            scan_ts = datetime.now(timezone.utc).isoformat()
            saved = 0
            now_utc = datetime.now(timezone.utc)

            # Track unique games at GAME level, not opportunity level
            skipped_event_ids: set[str] = set()
            analyzed_event_ids: set[str] = set()
            skipped_opp_count = 0  # opportunity-level count for deduped calc

            for opp in all_opps:
                eid = opp.get("event_id", "")
                gi = game_info.get(eid, {})
                event_status = gi.get("status", "")
                start_time = gi.get("start_time", "")
                matchup = _build_matchup(gi.get("away_team", ""), gi.get("home_team", ""))

                # Live-game filtering
                skippable, reason = _is_game_skippable(
                    event_status, start_time, now_utc=now_utc
                )
                if skippable:
                    skipped_opp_count += 1
                    if eid not in skipped_event_ids:
                        skipped_event_ids.add(eid)
                        state.skipped_games.append({
                            "matchup": matchup or eid,
                            "start_time": start_time,
                            "status": event_status,
                            "reason": reason,
                            "event_id": eid,
                        })
                    continue

                is_yn = opp.get("line") is None and opp.get("comparison_status") is not None
                if eid not in analyzed_event_ids:
                    analyzed_event_ids.add(eid)
                    state.n_games_analyzed += 1
                rec = {
                    "scan_run_id": state.scan_run_id or None,
                    "ingestion_run_id": state.ingestion_run_id or None,
                    "event_id": eid,
                    "event_start_time": opp.get("start_time", "") or start_time,
                    "player_id": opp["player_id"],
                    "player_name": opp.get("player_name", ""),
                    "market_type": opp["market_type"],
                    "market_form": "yn" if is_yn else "ou",
                    "period": "game",
                    "line": opp.get("line"),
                    "side": opp["side"],
                    "sportsbook": opp["sportsbook"],
                    "offered_american_odds": opp["american_odds"],
                    "offered_decimal_odds": opp["decimal_odds"],
                    "offered_implied_prob": 0.0,
                    "fair_prob": opp.get("fair_prob"),
                    "fair_american_odds": None,
                    "ev_pct": opp.get("ev_pct"),
                    "yn_reference_prob": opp.get("market_reference_probability"),
                    "yn_reference_odds": opp.get("market_reference_odds"),
                    "yn_implied_prob_adv": opp.get("price_advantage_pct"),
                    "yn_decimal_odds_adv": opp.get("decimal_odds_advantage"),
                    "n_consensus_books": opp.get("n_consensus_books"),
                    "market_quality": opp.get("market_quality", ""),
                    "rec_status": opp.get("bet_status", opp.get("comparison_status", "")),
                    "rec_eligible": opp.get("rec_eligible", False),
                    "pinnacle_approved": opp.get("pinnacle_approved"),
                    "is_official": opp.get("is_official", False),
                    "data_source": state.data_source,
                    "observation_timestamp": scan_ts,
                    "scan_timestamp": scan_ts,
                    "freshness_status": "STALE" if state.stale_warning else "FRESH",
                    "model_version": state.version,
                    "matchup": matchup,
                    "event_status": event_status,
                }

                # Compute implied probability if not set
                if rec["offered_implied_prob"] == 0.0:
                    rec["offered_implied_prob"] = american_to_probability(
                        rec["offered_american_odds"]
                    )

                # Compute fair American odds from fair probability
                if rec["fair_prob"] is not None and rec["fair_american_odds"] is None:
                    rec["fair_american_odds"] = probability_to_american(rec["fair_prob"])

                # Compute Model Score
                try:
                    from src.model_scoring import compute_model_score
                    score_result = compute_model_score(rec)
                    rec["model_score"] = score_result.score
                    rec["score_version"] = score_result.version
                    rec["score_components"] = json.dumps(score_result.components)
                    rec["score_cap"] = score_result.applied_cap
                    rec["score_explanation"] = score_result.explanation
                    # Score diagnostics
                    rec["points_to_7"] = score_result.points_to_7
                    rec["price_outlier_capped"] = 1 if score_result.price_outlier_capped else 0
                    rec["true_ev_unavailable"] = 1 if score_result.true_ev_unavailable else 0
                    rec["one_sided_market"] = 1 if score_result.one_sided_market else 0
                    rec["insufficient_books_failure"] = 1 if score_result.insufficient_books_failure else 0
                except Exception:
                    rec["model_score"] = None
                    rec["score_version"] = None
                    rec["score_components"] = None
                    rec["score_cap"] = None
                    rec["score_explanation"] = None
                    rec["points_to_7"] = 0.0
                    rec["price_outlier_capped"] = 0
                    rec["true_ev_unavailable"] = 0
                    rec["one_sided_market"] = 0
                    rec["insufficient_books_failure"] = 0

                # Compute Market Quality Score
                try:
                    from src.market_quality import compute_market_quality_score
                    has_both = rec.get("fair_prob") is not None and rec.get("market_form") == "ou"
                    mqs_result = compute_market_quality_score(rec, has_both_sides=has_both)
                    rec["market_quality_score"] = mqs_result.score
                except Exception:
                    rec["market_quality_score"] = 0.0

                # Classify tier (OFFICIAL_TRACKED, DISCOVERY_TRACKED, or RESEARCH_ONLY)
                try:
                    from src.official_picks import classify_recommendation
                    qual = classify_recommendation(rec)
                    rec.update(qual.to_dict())
                except Exception:
                    rec["recommendation_tier"] = "RESEARCH_ONLY"
                    rec["qualification_passed"] = 0
                    rec["qualification_reasons"] = ""
                    rec["disqualification_reasons"] = ""
                    rec["contributing_book_count"] = 0
                    rec["contributing_books"] = ""
                    rec["applicable_edge_metric"] = ""
                    rec["applicable_edge_threshold"] = 0.0
                    rec["model_score_threshold"] = 8.0
                    rec["qualification_rules_version"] = ""

                result_id = save_recommendation(conn, rec)
                if result_id is not None:
                    saved += 1

            state.n_recommendations_saved = saved
            state.n_games_skipped = len(skipped_event_ids)
            deduped = len(all_opps) - skipped_opp_count - saved
            print(f"  Saved: {saved}  |  Skipped (live/completed): {state.n_games_skipped} game(s) ({skipped_opp_count} opps)  |  Deduplicated: {deduped}")

            if state.skipped_games:
                print("  Skipped games:")
                for sg in state.skipped_games:
                    print(f"    - {sg['matchup']}: {sg['reason']}")

            # Phase 16: Rank and select official picks
            if saved > 0:
                try:
                    from src.official_picks import rank_and_select_official_picks
                    today_recs = conn.execute(
                        """SELECT * FROM historical_recommendations
                           WHERE date(scan_timestamp) = date('now')
                           AND event_status NOT IN (
                               'live','inprogress','in_progress','started','in-progress',
                               'final','finished','completed','closed','ended'
                           )"""
                    ).fetchall()
                    today_recs = [dict(r) for r in today_recs]
                    official = rank_and_select_official_picks(today_recs)
                    if official:
                        from database.db_manager import freeze_official_pick
                        n_frozen = 0
                        for rank, rec in enumerate(official, 1):
                            if freeze_official_pick(
                                conn,
                                rec["recommendation_id"],
                                tier=rec.get("recommendation_tier", "OFFICIAL_TRACKED"),
                                official_rank=rank,
                            ):
                                n_frozen += 1
                        state.n_official_picks = n_frozen
                        print(f"  Official picks frozen: {n_frozen}")
                    else:
                        state.n_official_picks = 0
                        print("  Official picks frozen: 0 (none qualified)")
                except Exception as e:
                    state.n_official_picks = 0
                    print(f"  Official picks selection failed: {e}")

            # Capture closing prices for saved recommendations
            if saved > 0:
                run_recs = conn.execute(
                    """SELECT * FROM historical_recommendations
                       WHERE scan_run_id = ?""",
                    (state.scan_run_id,),
                ).fetchall()
                run_recs = [dict(r) for r in run_recs]
                n_captured = capture_closing_prices(conn, run_recs)
                if n_captured > 0:
                    print(f"  Closing prices captured: {n_captured}")

            # Validation: check for live-game recommendations
            if saved > 0:
                row = conn.execute(
                    """SELECT COUNT(*) AS cnt FROM historical_recommendations
                       WHERE scan_run_id = ?
                       AND event_status IN ('live','inprogress','in_progress',
                           'started','in-progress','final','finished',
                           'completed','closed','ended')""",
                    (state.scan_run_id,),
                ).fetchone()
                live_check = dict(row).get("cnt", 0) if row else 0
                if live_check > 0:
                    state.has_live_game_recs = True
                    state.errors.append(
                        f"VALIDATION FAILURE: {live_check} recommendations from live/completed games"
                    )
                    state.n_errors += 1
                    print(f"  VALIDATION FAILURE: {live_check} recs from live/completed games", file=sys.stderr)

        finally:
            conn.close()

    except Exception as exc:
        state.errors.append(f"Freeze failed: {exc}")
        state.n_errors += 1
        print(f"  ERROR: {exc}", file=sys.stderr)
        state.stage_timings["freeze"] = round(time.monotonic() - t0, 3)
        return False

    state.stage_timings["freeze"] = round(time.monotonic() - t0, 3)
    return True


# ==================================================================
# Stage 8: Produce reports
# ==================================================================

def _stage_reports(config: PipelineConfig, state: PipelineState) -> bool:
    """Generate output reports. Returns True on success."""
    print("[8/9] Producing reports")
    t0 = time.monotonic()

    output_dir = Path(config.output_dir)
    if not config.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    ou_opps = state.scan_result.get("opportunities", [])
    yn_opps = state.scan_result.get("yn_opportunities", [])
    all_opps = ou_opps + yn_opps

    # ── recommendations.csv ──
    if all_opps:
        csv_path = output_dir / "recommendations.csv"
        _write_csv(csv_path, all_opps, config.dry_run)
        print(f"  {csv_path}" + (" (dry run)" if config.dry_run else ""))

    # ── recommendations.json ──
    if all_opps:
        json_path = output_dir / "recommendations.json"
        _write_json(json_path, all_opps, config.dry_run)
        print(f"  {json_path}" + (" (dry run)" if config.dry_run else ""))

    # ── run_summary.json ──
    summary = _build_run_summary(state)
    summary_path = output_dir / "run_summary.json"
    _write_json_dict(summary_path, summary, config.dry_run)
    print(f"  {summary_path}" + (" (dry run)" if config.dry_run else ""))

    # ── pipeline_report.txt ──
    report = _build_pipeline_report(state)
    report_path = output_dir / "pipeline_report.txt"
    _write_text(report_path, report, config.dry_run)
    print(f"  {report_path}" + (" (dry run)" if config.dry_run else ""))

    state.stage_timings["reports"] = round(time.monotonic() - t0, 3)
    return True


# ==================================================================
# Stage 9: Print summary
# ==================================================================

def _stage_summary(config: PipelineConfig, state: PipelineState) -> None:
    """Print terminal summary."""
    print("[9/9] Pipeline summary")
    elapsed = (datetime.now(timezone.utc) - state.start_time).total_seconds()

    status_label = state.status
    if state.status == "RUNNING":
        if state.n_recommendations_saved > 0:
            status_label = "SUCCESS"
        elif state.n_ou_opportunities + state.n_yn_opportunities == 0:
            status_label = "SUCCESS_NO_RECS"
        else:
            status_label = "SUCCESS"

    print()
    print("=" * 60)
    print(f"  Pipeline Status:    {status_label}")
    print(f"  Run ID:             {state.pipeline_run_id[:8]}...")
    print(f"  Events Processed:   {state.n_events}")
    print(f"  Games Total:        {state.n_total_games}")
    print(f"  Games Analyzed:     {state.n_games_analyzed}")
    print(f"  Games Skipped:      {state.n_games_skipped}")
    print(f"  Markets Scanned:    {state.n_markets}")
    print(f"  Books Found:        {state.n_books}")
    print(f"  Approved Rows:      {state.n_approved_rows}")
    print(f"  O/U Opportunities:  {state.n_ou_opportunities}")
    print(f"  YN Opportunities:   {state.n_yn_opportunities}")
    print(f"  Recommendations:    {state.n_recommendations_saved}")
    print(f"  Errors:             {state.n_errors}")
    print(f"  Warnings:           {state.n_warnings}")
    print(f"  Elapsed:            {elapsed:.1f}s")
    print("=" * 60)

    if state.warnings:
        print()
        for w in state.warnings:
            print(f"  WARNING: {w}")
    if state.errors:
        print()
        for e in state.errors:
            print(f"  ERROR: {e}")

    # Stage timings
    if state.stage_timings:
        print()
        print("  Stage Timings:")
        for stage_name, duration in state.stage_timings.items():
            print(f"    {stage_name:<20} {duration:.3f}s")

    print()


# ==================================================================
# Report builders
# ==================================================================

def _build_run_summary(state: PipelineState) -> dict:
    """Build run_summary.json content."""
    return {
        "pipeline_run_id": state.pipeline_run_id,
        "start_time": state.start_time.isoformat(),
        "version": state.version,
        "status": state.status,
        "execution_mode": state.execution_mode,
        "config_summary": state.config_summary,
        "metrics": {
            "n_events": state.n_events,
            "n_markets": state.n_markets,
            "n_books": state.n_books,
            "n_approved_rows": state.n_approved_rows,
            "n_excluded_rows": state.n_excluded_rows,
            "n_ou_opportunities": state.n_ou_opportunities,
            "n_yn_opportunities": state.n_yn_opportunities,
            "n_recommendations_saved": state.n_recommendations_saved,
            "n_total_games": state.n_total_games,
            "n_games_analyzed": state.n_games_analyzed,
            "n_games_skipped": state.n_games_skipped,
            "n_errors": state.n_errors,
            "n_warnings": state.n_warnings,
        },
        "skipped_games": state.skipped_games,
        "data_source": state.data_source,
        "stale_warning": state.stale_warning,
        "research_only": state.research_only,
        "stage_timings": state.stage_timings,
        "pinnacle_diagnostics": state.scan_result.get("pinnacle_diagnostics", {}),
        "errors": state.errors,
        "warnings": state.warnings,
    }


def _build_pipeline_report(state: PipelineState) -> str:
    """Build pipeline_report.txt content."""
    elapsed = (datetime.now(timezone.utc) - state.start_time).total_seconds()
    lines = [
        "MLB Sportsbook Analysis Pipeline Report",
        "=" * 50,
        f"Run ID:      {state.pipeline_run_id}",
        f"Started:     {state.start_time.isoformat()}",
        f"Version:     {state.version}",
        f"Status:      {state.status}",
        f"Mode:        {state.execution_mode}",
        f"Elapsed:     {elapsed:.1f}s",
        "",
        "--- Configuration ---",
    ]
    for k, v in state.config_summary.items():
        lines.append(f"  {k}: {v}")

    lines.extend([
        "",
        "--- Metrics ---",
        f"  Events:              {state.n_events}",
        f"  Markets:             {state.n_markets}",
        f"  Books:               {state.n_books}",
        f"  Approved Rows:       {state.n_approved_rows}",
        f"  Excluded Rows:       {state.n_excluded_rows}",
        f"  O/U Opportunities:   {state.n_ou_opportunities}",
        f"  YN Opportunities:    {state.n_yn_opportunities}",
        f"  Recommendations:     {state.n_recommendations_saved}",
        f"  Errors:              {state.n_errors}",
        f"  Warnings:            {state.n_warnings}",
        f"  Data Source:         {state.data_source}",
        f"  Stale Warning:       {state.stale_warning}",
        f"  Research Only:       {state.research_only}",
    ])

    if state.stage_timings:
        lines.extend(["", "--- Stage Timings ---"])
        for name, dur in state.stage_timings.items():
            lines.append(f"  {name:<20} {dur:.3f}s")

    if state.warnings:
        lines.extend(["", "--- Warnings ---"])
        for w in state.warnings:
            lines.append(f"  - {w}")

    if state.errors:
        lines.extend(["", "--- Errors ---"])
        for e in state.errors:
            lines.append(f"  - {e}")

    lines.append("")
    lines.append("=" * 50)
    lines.append("End of report")
    return "\n".join(lines)


# ==================================================================
# File writers
# ==================================================================

def _write_csv(path: Path, opps: list[dict], dry_run: bool) -> None:
    """Write opportunities to CSV."""
    if not opps:
        return
    if dry_run:
        return

    # Union of all keys
    all_keys: list[str] = []
    seen: set[str] = set()
    for opp in opps:
        for k in opp:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(opps)


def _write_json(path: Path, data: Any, dry_run: bool) -> None:
    """Write data to JSON file."""
    if dry_run:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _write_json_dict(path: Path, data: dict, dry_run: bool) -> None:
    """Write dict to JSON file."""
    if dry_run:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def _write_text(path: Path, content: str, dry_run: bool) -> None:
    """Write text to file."""
    if dry_run:
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ==================================================================
# Helpers
# ==================================================================

def _parse_status(status_obj: dict | str) -> str:
    """Normalize game status string."""
    if isinstance(status_obj, str):
        return status_obj or "scheduled"
    if isinstance(status_obj, dict):
        return status_obj.get("state", "scheduled")
    return "scheduled"


# ==================================================================
# Completion flag
# ==================================================================

_PIPELINE_COMPLETION_FILE = Path(__file__).resolve().parent.parent / "database" / ".pipeline_completed"

def _write_completion_flag(config: PipelineConfig, state: PipelineState) -> None:
    """Write a timestamp file so the dashboard can show when the pipeline last ran."""
    try:
        flag = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": state.run_id,
            "n_recommendations": state.n_recommendations_saved,
            "exit_code": EXIT_SUCCESS if state.n_recommendations_saved > 0 else EXIT_SUCCESS_NO_RECS,
        }
        _PIPELINE_COMPLETION_FILE.write_text(json.dumps(flag, indent=2))
    except Exception:
        pass  # non-critical, don't fail the pipeline


# ==================================================================
# Main pipeline
# ==================================================================

def run_pipeline(config: PipelineConfig) -> int:
    """Execute the full pipeline. Returns exit code."""
    state = PipelineState()

    try:
        # Stage 1: Validate
        if not _stage_validate_config(config, state):
            return EXIT_CONFIG_FAILURE

        # Stage 2: Create run
        _stage_create_run(config, state)

        # Stage 3: Fetch
        if not _stage_fetch_events(config, state):
            return EXIT_API_FAILURE

        # Stage 4: Ingest
        if not _stage_ingest(config, state):
            return EXIT_DB_FAILURE

        # Stage 5: Validate
        if not _stage_validate(config, state):
            return EXIT_VALIDATION_FAILURE

        # Stage 6: Scan
        if not _stage_scan(config, state):
            return EXIT_DB_FAILURE

        # Stage 7: Freeze
        if not _stage_freeze(config, state):
            return EXIT_DB_FAILURE

        # Validate: fail if live-game recommendations slipped through
        if state.has_live_game_recs:
            state.status = "VALIDATION_FAILURE"
            return EXIT_VALIDATION_FAILURE

        # Stage 8: Reports
        _stage_reports(config, state)

        # Stage 9: Summary
        _stage_summary(config, state)

        # Write pipeline completion flag
        _write_completion_flag(config, state)

        # Determine exit code
        if state.n_recommendations_saved > 0:
            return EXIT_SUCCESS
        elif state.n_ou_opportunities + state.n_yn_opportunities == 0:
            return EXIT_SUCCESS_NO_RECS
        else:
            return EXIT_SUCCESS

    except Exception as exc:
        state.errors.append(f"Unexpected failure: {exc}")
        state.status = "UNEXPECTED_FAILURE"
        state.n_errors += 1
        logger.exception("Pipeline failed unexpectedly")
        print(f"\nFATAL: Unexpected error — {exc}", file=sys.stderr)
        _stage_summary(config, state)
        return EXIT_UNEXPECTED_FAILURE


# ==================================================================
# CLI
# ==================================================================

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="daily_pipeline",
        description="MLB sportsbook analysis — daily production pipeline",
    )
    # Data source
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--live", action="store_true",
                        help="Fetch live data from API (default: use cache)")
    source.add_argument("--cache", action="store_true",
                        help="Use cached data (default behavior)")
    source.add_argument("--auto", action="store_true",
                        help="Use live if available, fall back to cache")

    # Market selection
    from src.prop_config import MARKET_REGISTRY
    _valid_markets = [m.cli_name for m in MARKET_REGISTRY]
    parser.add_argument("--market", default="all",
                        choices=_valid_markets + ["all"],
                        help="Market to scan (default: all)")
    parser.add_argument("--market-form", default="all",
                        choices=["ou", "yn", "all"],
                        help="Market form (default: all)")

    # Mode
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--actionable-only", action="store_true", default=False,
                            help="Show only actionable opportunities")
    mode_group.add_argument("--positive-only", action="store_true",
                            help="Show only positive-EV opportunities")
    mode_group.add_argument("--all-markets", action="store_true",
                            help="Show all markets (no filtering)")

    # Output
    parser.add_argument("--output-dir", default="output",
                        help="Report output directory (default: output)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run all stages except database writes")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Output summary as JSON")
    parser.add_argument("--csv", action="store_true", dest="as_csv",
                        help="Output summary as CSV")

    # Safety
    parser.add_argument("--require-fresh", action="store_true",
                        help="Exit nonzero if data is stale")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Determine mode
    actionable_only = True
    positive_only = False
    if args.positive_only:
        actionable_only = False
        positive_only = True
    elif args.all_markets:
        actionable_only = False

    config = PipelineConfig(
        live=args.live,
        use_cache=args.cache,
        auto=args.auto,
        output_dir=args.output_dir,
        market=args.market,
        market_form=args.market_form,
        actionable_only=actionable_only,
        positive_only=positive_only,
        require_fresh=args.require_fresh,
        dry_run=args.dry_run,
        as_json=args.as_json,
        as_csv=args.as_csv,
        debug=args.debug,
    )

    if config.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

    return run_pipeline(config)


if __name__ == "__main__":
    sys.exit(main())
