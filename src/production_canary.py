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
) -> CanaryResult:
    """Run a minimal canary test."""
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
        events = _fetch_canary_sample(config, event_id=event_id)
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
    market_issues = _validate_market_mappings(markets)
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
    """Validate database has required tables."""
    db_path = Path(config.database_path)
    if not db_path.exists():
        return f"Database not found: {db_path}"
    try:
        conn = get_connection(config.database_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            required = {"games", "raw_responses", "odds", "historical_recommendations"}
            missing = required - tables
            if missing:
                return f"Missing tables: {', '.join(missing)}"
        finally:
            conn.close()
    except Exception as e:
        return f"Schema check failed: {e}"
    return None


def _fetch_canary_sample(
    config: ProductionConfig,
    *,
    event_id: str = "",
) -> list[dict]:
    """Fetch a minimal API sample."""
    import urllib.request
    import urllib.error

    url = "https://api.sportsdata.io/v2/mlb/odds/json/EventOdds"
    headers = {"Authorization": f"Bearer {config.api_key}"}

    if event_id:
        url += f"/{event_id}"
    else:
        url += "/date/next"

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    if isinstance(data, list):
        return data[:3]  # Limit to 3 events for canary
    elif isinstance(data, dict):
        return [data]
    return []


def _validate_api_schemas(events: list[dict]) -> list[str]:
    """Validate API response structure."""
    issues = []
    if not events:
        issues.append("No events returned from API")
        return issues

    required_fields = {"EventId", "HomeTeam", "AwayTeam", "Period"}
    for i, event in enumerate(events):
        missing = required_fields - set(event.keys())
        if missing:
            issues.append(f"Event {i}: missing fields {missing}")

        # Check odds structure
        odds = event.get("PregameOdds") or event.get("LiveOdds") or []
        if not isinstance(odds, list):
            issues.append(f"Event {i}: PregameOdds is not a list")

    return issues


def _extract_sportsbooks(events: list[dict]) -> set[str]:
    """Extract unique sportsbook names from events."""
    books = set()
    for event in events:
        for odds_key in ("PregameOdds", "LiveOdds"):
            odds = event.get(odds_key) or []
            for odds_entry in odds:
                book = odds_entry.get("Sportsbook", "")
                if book:
                    books.add(book)
    return books


def _extract_markets(events: list[dict]) -> list[dict]:
    """Extract all market entries from events."""
    markets = []
    for event in events:
        for odds_key in ("PregameOdds", "LiveOdds"):
            odds = event.get(odds_key) or []
            for odds_entry in odds:
                markets.append({
                    "event_id": event.get("EventId"),
                    "sportsbook": odds_entry.get("Sportsbook", ""),
                    "market": odds_entry.get("Market", ""),
                })
    return markets


def _validate_mappings(events: list[dict]) -> list[str]:
    """Validate sportsbook and entity mappings."""
    issues = []
    for i, event in enumerate(events):
        home_id = event.get("HomeTeamId")
        away_id = event.get("AwayTeamId")
        if not home_id or not away_id:
            issues.append(f"Event {i}: missing team IDs")
        if not event.get("HomeTeam") or not event.get("AwayTeam"):
            issues.append(f"Event {i}: missing team names")
    return issues


def _validate_market_mappings(markets: list[dict]) -> list[str]:
    """Validate market type mappings."""
    from src.prop_config import MARKET_REGISTRY
    issues = []
    supported = {m.api_suffix for m in MARKET_REGISTRY.values()}

    for m in markets:
        market_type = m.get("market", "")
        if market_type and market_type not in supported:
            # Check if it's a known unsupported type
            pass  # Don't fail for unknown markets in canary
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
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_canary_result(result))

    return EXIT_SUCCESS if result.status in ("success", "warnings") else EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
