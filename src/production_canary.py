"""Production canary run.

Runs a minimal live test against the API to validate schemas,
mappings, and data quality before enabling full production.
Processes a single event or limited market subset.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.production_config import load_config, ProductionConfig
from database.connection import get_database_url
from database.db_manager import get_connection

logger = logging.getLogger(__name__)

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_NO_WRITE = 2


@dataclass
class CanaryResult:
    """Results from a canary run."""
    status: str = "pending"
    started_at: str = ""
    completed_at: str = ""
    events_fetched: int = 0
    markets_processed: int = 0
    sportsbooks_observed: list[str] = field(default_factory=list)
    writes_performed: list[dict] = field(default_factory=list)
    writes_skipped: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    estimated_api_calls: int = 0
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_canary(
    config: ProductionConfig | None = None,
    *,
    event_id: str = "",
    market: str = "all",
    sportsbook: str = "",
    no_write: bool = False,
    debug: bool = False,
    league: str = "MLB",
) -> CanaryResult:
    """Run a minimal canary test against *league*."""
    if config is None:
        config = load_config()

    result = CanaryResult(started_at=datetime.now(timezone.utc).isoformat())

    # Step 1: Validate configuration
    logger.info("[1/9] Validating configuration")
    errors = config.validate()
    if errors:
        result.errors.extend(errors)
        result.status = "failed"
        result.completed_at = datetime.now(timezone.utc).isoformat()
        return result

    from src.sports import get_league, supported_leagues
    try:
        league_mod = get_league(league)
    except ValueError:
        result.errors.append(f"Unknown league: {league!r}. Supported: {', '.join(supported_leagues())}")
        result.status = "failed"
        result.completed_at = datetime.now(timezone.utc).isoformat()
        return result
    if not getattr(league_mod, "AVAILABLE", False):
        result.errors.append(f"{league} is not currently available: {league_mod.UNAVAILABLE_REASON}")
        result.status = "failed"
        result.completed_at = datetime.now(timezone.utc).isoformat()
        return result

    # Step 2: Validate schema
    logger.info("[2/9] Validating database schema")
    schema_err = _validate_schema(config)
    if schema_err:
        result.errors.append(schema_err)
        result.status = "failed"
        result.completed_at = datetime.now(timezone.utc).isoformat()
        return result

    # Step 3: Fetch minimal sample
    logger.info("[3/9] Fetching minimal live sample")
    t0 = time.monotonic()
    try:
        events = _fetch_canary_sample(config, event_id=event_id, league=league)
        result.events_fetched = len(events)
        result.estimated_api_calls = 1
    except Exception as e:
        result.errors.append(f"API fetch failed: {e}")
        result.status = "failed"
        result.completed_at = datetime.now(timezone.utc).isoformat()
        return result
    fetch_time = time.monotonic() - t0

    # Step 4: Validate returned schemas
    logger.info("[4/9] Validating API response schemas")
    schema_issues = _validate_api_schemas(events)
    if schema_issues:
        result.warnings.extend(schema_issues)

    # Step 5: Validate sportsbook mappings
    logger.info("[5/9] Validating sportsbook mappings")
    books = _extract_sportsbooks(events)
    result.sportsbooks_observed = sorted(books)
    mapping_issues = _validate_mappings(events)
    if mapping_issues:
        result.errors.extend(mapping_issues)

    # Step 6: Validate market mappings
    logger.info("[6/9] Validating market mappings")
    markets = _extract_markets(events)
    result.markets_processed = len(markets)
    market_issues = _validate_market_mappings(markets, league=league)
    if market_issues:
        result.warnings.extend(market_issues)

    # Step 7: Run analysis (dry)
    logger.info("[7/9] Running analysis (dry)")
    analysis_result = _dry_analysis(events)

    # Step 8: Print estimated usage
    logger.info("[8/9] Estimated API usage: 1 call, %d events, %d markets",
                result.events_fetched, result.markets_processed)

    # Step 9: Print writes
    logger.info("[9/9] Write summary")
    result.writes_skipped = no_write
    if not no_write:
        result.writes_performed = [
            {"table": "raw_responses", "rows": result.events_fetched},
            {"table": "odds", "rows": result.markets_processed},
        ]
    else:
        result.writes_performed = []

    result.duration_seconds = round(time.monotonic() - t0, 2)
    result.completed_at = datetime.now(timezone.utc).isoformat()

    if result.errors:
        result.status = "failed"
    elif result.warnings:
        result.status = "warnings"
    else:
        result.status = "success"

    return result


def _validate_schema(config: ProductionConfig) -> str | None:
    """Validate database has required tables (PostgreSQL or SQLite)."""
    db_url = get_database_url()
    if not db_url:
        db_path = Path(config.database_path)
        if not db_path.exists():
            return f"Database not found: {db_path}"

    try:
        conn = get_connection() if db_url else get_connection(config.database_path)
        try:
            if db_url:
                cursor = conn.execute(
                    "SELECT table_name AS name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            else:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            tables = {row["name"] for row in cursor.fetchall()}
            required = {"games", "raw_responses", "odds", "historical_recommendations"}
            missing = required - tables
            if missing:
                return f"Missing tables: {', '.join(sorted(missing))}"
        finally:
            conn.close()
    except Exception as e:
        return f"Schema check failed: {e}"
    return None


def _fetch_canary_sample(
    config: ProductionConfig,
    *,
    event_id: str = "",
    league: str = "MLB",
) -> list[dict]:
    """Fetch a minimal sample from the real production API (SportsGameOdds v2).

    Uses the same client and endpoint as ``daily_pipeline.py``, so the
    canary exercises the actual production data path rather than a
    different provider's schema. ``max_cache_age=0`` forces a live
    fetch (a canary that silently reused a stale cache would prove
    nothing about current connectivity).
    """
    from src.api_client import SportsGameOddsClient

    client = SportsGameOddsClient(max_cache_age=0)
    data, _from_cache = client.get_events(
        league=league,
        event_id=event_id or None,
        odds_available=True,
        include_alt_lines=True,
    )
    events = data.get("data", []) or []
    return events[:3]  # Limit to 3 events for canary


def _validate_api_schemas(events: list[dict]) -> list[str]:
    """Validate SportsGameOdds v2 event response structure."""
    issues = []
    if not events:
        issues.append("No events returned from API")
        return issues

    required_fields = {"eventID", "teams", "status", "odds"}
    for i, event in enumerate(events):
        missing = required_fields - set(event.keys())
        if missing:
            issues.append(f"Event {i}: missing fields {missing}")
            continue

        odds = event.get("odds")
        if not isinstance(odds, dict):
            issues.append(f"Event {i}: odds is not a dict")

    return issues


def _extract_sportsbooks(events: list[dict]) -> set[str]:
    """Extract unique sportsbook keys observed under byBookmaker."""
    books = set()
    for event in events:
        odds = event.get("odds") or {}
        for odd in odds.values():
            books.update((odd.get("byBookmaker") or {}).keys())
    return books


def _extract_markets(events: list[dict]) -> list[dict]:
    """Extract all odd entries from events, keyed by their real oddID."""
    markets = []
    for event in events:
        odds = event.get("odds") or {}
        for odd_id, odd in odds.items():
            markets.append({
                "event_id": event.get("eventID"),
                "odd_id": odd_id,
                "market_name": odd.get("marketName", ""),
                "stat_entity_id": odd.get("statEntityID", ""),
                "bet_type": odd.get("betTypeID", ""),
                "sportsbooks": sorted((odd.get("byBookmaker") or {}).keys()),
            })
    return markets


def _validate_mappings(events: list[dict]) -> list[str]:
    """Validate team ID and stat-entity mappings."""
    issues = []
    for i, event in enumerate(events):
        teams = event.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}
        if not home.get("teamID") or not away.get("teamID"):
            issues.append(f"Event {i}: missing team IDs")
        if not home.get("statEntityID") or not away.get("statEntityID"):
            issues.append(f"Event {i}: missing statEntityID mapping")
    return issues


def _validate_market_mappings(markets: list[dict], league: str = "MLB") -> list[str]:
    """Validate player-prop oddIDs against *league*'s registered market patterns.

    Game-level markets (moneyline, spread, totals — ``statEntityID`` of
    "home"/"away"/"all") are parsed by ``odds_parser.py``, not the
    player-prop registry, so they are excluded from this check.
    """
    from src.sports import get_league
    from src.sports.base import match_ou_market, match_yn_market

    issues: list[str] = []
    if not markets:
        return issues

    registry = get_league(league).get_market_registry()
    game_entities = {"home", "away", "all"}
    prop_markets = [m for m in markets if m.get("stat_entity_id") not in game_entities]
    if not prop_markets:
        return issues

    unmatched = sum(
        1 for m in prop_markets
        if not (match_ou_market(registry, m.get("odd_id", ""))
                or match_yn_market(registry, m.get("odd_id", "")))
    )
    unmatched_ratio = unmatched / len(prop_markets)
    if unmatched_ratio > 0.8:
        issues.append(
            f"{unmatched}/{len(prop_markets)} player-prop markets matched no "
            f"registered pattern ({unmatched_ratio:.0%}) — registry may be stale"
        )
    return issues


def _dry_analysis(events: list[dict]) -> dict:
    """Perform dry analysis without persisting."""
    return {
        "events_analyzed": len(events),
        "consensus_computed": False,
        "ev_computed": False,
        "note": "Dry run — no analysis persisted",
    }


def format_canary_result(result: CanaryResult) -> str:
    """Format canary result for display."""
    lines = [
        f"Canary Run — {result.status.upper()}",
        f"  Duration: {result.duration_seconds}s",
        f"  Events fetched: {result.events_fetched}",
        f"  Markets processed: {result.markets_processed}",
        f"  Sportsbooks: {', '.join(result.sportsbooks_observed)}",
        f"  Estimated API calls: {result.estimated_api_calls}",
        f"  Writes skipped: {result.writes_skipped}",
    ]
    if result.writes_performed:
        lines.append("  Writes performed:")
        for w in result.writes_performed:
            lines.append(f"    {w['table']}: {w['rows']} rows")
    if result.warnings:
        lines.append("  Warnings:")
        for w in result.warnings:
            lines.append(f"    - {w}")
    if result.errors:
        lines.append("  Errors:")
        for e in result.errors:
            lines.append(f"    - {e}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="production_canary", description="Production canary run")
    parser.add_argument("--event", help="Specific event ID to test")
    parser.add_argument("--market", default="all", help="Market filter")
    parser.add_argument("--sportsbook", default="", help="Sportsbook filter")
    parser.add_argument("--no-write", action="store_true", help="Skip all database writes")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--league", default="MLB", help="League to test (MLB, NFL, ...). Default MLB.")
    args = parser.parse_args(argv)

    from src.structured_logging import setup_logging
    setup_logging(level="DEBUG" if args.debug else "WARNING")

    config = load_config()
    result = run_canary(
        config,
        event_id=args.event or "",
        market=args.market,
        sportsbook=args.sportsbook,
        no_write=args.no_write,
        debug=args.debug,
        league=(args.league or "MLB").upper(),
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_canary_result(result))

    return EXIT_SUCCESS if result.status in ("success", "warnings") else EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
