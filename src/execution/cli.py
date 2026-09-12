"""Execution-layer CLI.

    python -m src.execution.cli check-connectivity
    python -m src.execution.cli inspect-markets --provider kalshi --limit 20 [--raw]
    python -m src.execution.cli scan-opportunities [--verbose] [--limit N] [--league MLB]
        [--provider kalshi] [--provider polymarket_us] [--analysis-stake 10]
    python -m src.execution.cli inventory-report [--provider kalshi]

All subcommands are read-only. None ever calls place_order/cancel_order/
modify_order or any write endpoint. Never displays credentials,
signatures, or private keys.
"""

from __future__ import annotations

import argparse
import copy
import sys
from decimal import Decimal
from typing import Any

from database.db_manager import get_connection
from src.execution import get_provider
from src.execution.base import Market
from src.execution.evaluator import (
    ExecutionOpportunity, OpportunityEvaluator, RejectionReason,
    build_execution_signal, reject_without_signal,
)
from src.execution.matching import (
    build_recommendation_event, find_best_match, provider_event_from_market,
)
from src.execution.opportunity_store import persist_opportunity, persist_rejection
from src.production_config import load_config

_SENSITIVE_KEY_PATTERNS = ("key", "secret", "signature", "token", "password", "credential", "auth")
_ALL_PROVIDERS = ("kalshi", "polymarket_us")


def _sanitize_raw(value: Any) -> Any:
    """Recursively redact anything credential-shaped. Market.raw comes
    from a public, read-only market-listing endpoint and shouldn't ever
    contain real credentials, but this is a defensive safety net, not
    an assumption that it's needed."""
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if any(p in k.lower() for p in _SENSITIVE_KEY_PATTERNS) else _sanitize_raw(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_raw(v) for v in value]
    return value


def _enabled_providers(config: Any) -> dict[str, bool]:
    return {"kalshi": config.kalshi_enabled, "polymarket_us": config.polymarket_us_enabled}


def _check_connectivity() -> int:
    config = load_config()
    any_failed = False
    for name, enabled in _enabled_providers(config).items():
        if not enabled:
            print(f"{name}: SKIPPED (disabled)")
            continue
        try:
            provider = get_provider(name, config)
            result = provider.health_check()
        except Exception as exc:
            print(f"{name}: FAIL ({type(exc).__name__}: {exc})")
            any_failed = True
            continue
        if result.ok:
            print(f"{name}: PASS ({result.detail})")
        else:
            print(f"{name}: FAIL ({result.detail})")
            any_failed = True

    return 1 if any_failed else 0


# ── inspect-markets ──────────────────────────────────────────────────

def _inspect_markets(provider_name: str, limit: int, raw: bool) -> int:
    config = load_config()
    if not _enabled_providers(config).get(provider_name):
        print(f"{provider_name}: verification still pending -- provider is disabled, no real data to inspect")
        return 1

    try:
        provider = get_provider(provider_name, config)
        markets = provider.get_markets(limit=limit)
    except Exception as exc:
        print(f"{provider_name}: verification still pending -- {type(exc).__name__}: {exc}")
        return 1

    print(f"provider={provider_name}")
    if not markets:
        print("0 markets returned")
        return 0

    for market in markets:
        event = provider.parse_game_event(market)
        print("-" * 40)
        print(f"event_id={market.id!r}")
        print(f"market_id={market.id!r}")
        print(f"title={market.title!r}")
        subtitle = market.raw.get("yes_sub_title") or market.raw.get("no_sub_title") or market.raw.get("subtitle")
        print(f"subtitle={subtitle!r}" if subtitle else "subtitle=UNVERIFIED (not present in this payload)")
        print(f"status={market.status!r}")
        for field_name in ("event_start_time", "close_time", "expiration_time"):
            value = market.raw.get(field_name)
            print(f"{field_name}={value!r}" if value else f"{field_name}=UNVERIFIED (not present in this payload)")
        yes_label = market.raw.get("yes_sub_title")
        no_label = market.raw.get("no_sub_title")
        print(f"yes_label={yes_label!r}" if yes_label else "yes_label=UNVERIFIED")
        print(f"no_label={no_label!r}" if no_label else "no_label=UNVERIFIED")
        try:
            book = provider.normalize_orderbook(provider.get_orderbook(market.id))
            best_bid = book.yes_bids[0].price if book.yes_bids else None
            best_ask = book.yes_asks[0].price if book.yes_asks else None
            print(f"best_bid={best_bid!r}")
            print(f"best_ask={best_ask!r}")
        except Exception as exc:
            print(f"best_bid=UNVERIFIED ({type(exc).__name__}: {exc})")
            print(f"best_ask=UNVERIFIED ({type(exc).__name__}: {exc})")
        if event is not None:
            print(
                f"parsed_away={event.away_team!r} parsed_home={event.home_team!r} "
                f"parsed_type={event.market_type!r} parsed_line={event.line!r}"
            )
        else:
            print("parsed=UNVERIFIED (unparseable by the current best-effort parser -- see parse_game_event)")
        if raw:
            print(f"raw={_sanitize_raw(market.raw)}")

    return 0


# ── inventory-report ─────────────────────────────────────────────────

def _detect_league_for_inventory(market: Market) -> str:
    """Best-effort, REPORTING-ONLY league detection from title/id/raw
    keyword hints -- not part of the matching-critical data model."""
    haystack = " ".join(
        str(x) for x in (
            market.id, market.title,
            market.raw.get("event_ticker", ""), market.raw.get("series_ticker", ""),
        )
    ).lower()
    if "nfl" in haystack:
        return "NFL"
    if "mlb" in haystack:
        return "MLB"
    if "wnba" in haystack:
        return "WNBA"
    return "Other"


def _classify_market_for_inventory(market: Market) -> str:
    """Best-effort, REPORTING-ONLY classification. Deliberately NOT
    parse_game_event (which only ever returns moneyline-or-None and is
    matching-critical) -- this bucket count must never be mistaken for
    authoritative classification."""
    title = (market.title or "").lower()
    raw = market.raw or {}

    if any(k in title for k in ("champion", "futures", "win the", "division")):
        return "futures"
    if any(k in raw for k in ("spread", "handicap")) or "spread" in title:
        return "spread"
    if "total" in title or "over/under" in title or "o/u" in title:
        return "total"
    prop_words = (
        "yards", "points", "rebounds", "assists", "strikeouts", "hits",
        "home run", "touchdown", "receptions", "passing", "rushing",
    )
    if any(w in title for w in prop_words):
        return "player_prop"
    if " vs" in title or " @ " in title:
        return "moneyline"
    return "unclassified"


def _inventory_report(only_providers: list[str] | None) -> int:
    config = load_config()
    targets = only_providers or list(_ALL_PROVIDERS)

    for name in targets:
        print(f"=== {name.upper()} ===")
        if not _enabled_providers(config).get(name):
            print("  SKIPPED (disabled)")
            continue
        try:
            provider = get_provider(name, config)
            markets = provider.get_markets(limit=200)
        except Exception as exc:
            print(f"  FAILED to fetch markets: {type(exc).__name__}: {exc}")
            continue

        print(f"  Open markets: {len(markets)}")

        parsed_count = 0
        unparsed_count = 0
        type_buckets = {"moneyline": 0, "spread": 0, "total": 0, "player_prop": 0, "futures": 0, "unclassified": 0}
        sport_buckets: dict[str, int] = {}

        for market in markets:
            if provider.parse_game_event(market) is not None:
                parsed_count += 1
            else:
                unparsed_count += 1
            type_buckets[_classify_market_for_inventory(market)] += 1
            sport = _detect_league_for_inventory(market)
            sport_buckets[sport] = sport_buckets.get(sport, 0) + 1

        total_parsed = parsed_count + unparsed_count
        success_pct = (parsed_count / total_parsed * 100) if total_parsed else 0.0
        print(f"  Parsed: {parsed_count}")
        print(f"  Unparsed: {unparsed_count}")
        print(f"  Parser success: {success_pct:.1f}%")

        print("  Sports:")
        for sport, count in sorted(sport_buckets.items(), key=lambda kv: -kv[1]):
            print(f"    {sport}: {count}")

        print("  Market types:")
        for bucket, count in type_buckets.items():
            print(f"    {bucket.replace('_', ' ').title()}: {count}")

    return 0


# ── scan-opportunities ───────────────────────────────────────────────

def _load_actionable_rows(config: Any) -> list[dict]:
    conn = get_connection(config.database_path)
    try:
        statuses = config.execution_allowed_rec_statuses_list()
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM historical_recommendations WHERE rec_status IN ({placeholders})",
            statuses,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _print_opportunity(result: ExecutionOpportunity) -> None:
    print(f"{result.provider.upper()}")
    print(f"  Match: {result.match_confidence * 100:.1f}%")
    print(f"  Best ask: {result.best_ask}")
    print(f"  Expected fill: {result.expected_fill_price}")
    print(f"  Fees: ${result.estimated_fees}")
    print(f"  Liquidity: ${result.available_liquidity}")
    print(f"  Raw EV: {result.raw_ev_pct:+.2f}%")
    print(f"  Net EV: {result.net_ev_pct:+.2f}%")


def _persist_comparison_results(conn: Any, comparison) -> None:
    """Persists EVERY provider's evaluation, not just the winner --
    needed for later provider-performance analysis (item 14)."""
    if comparison.best is not None:
        persist_opportunity(conn, comparison.best)
    for alt in comparison.qualified_alternatives:
        persist_opportunity(conn, alt)
    for rejection in comparison.provider_rejections:
        persist_rejection(conn, rejection)


def _scan_opportunities(
    verbose: bool, limit: int | None, league: str | None,
    only_providers: list[str] | None, analysis_stake: float | None,
) -> int:
    config = load_config()
    if analysis_stake is not None:
        config = copy.copy(config)
        config.execution_analysis_stake_usd = analysis_stake

    enabled = _enabled_providers(config)
    target_names = only_providers or list(_ALL_PROVIDERS)

    providers: dict[str, Any] = {}
    for name in target_names:
        if not enabled.get(name):
            continue
        try:
            providers[name] = get_provider(name, config)
        except Exception as exc:
            print(f"{name}: could not initialize ({type(exc).__name__}: {exc}), skipping")

    if not providers:
        print("No providers enabled -- nothing to scan.")
        return 0

    rows = _load_actionable_rows(config)
    if league:
        rows = [r for r in rows if (r.get("league") or "").upper() == league.upper()]
    if limit is not None:
        rows = rows[:limit]

    evaluator = OpportunityEvaluator(config)

    # Fetch each provider's candidate markets once per run, not once per
    # recommendation -- get_markets() is a real network call. One
    # provider failing to fetch never blocks the others (item 16).
    provider_events: dict[str, list] = {}
    for name, provider in providers.items():
        try:
            candidates = provider.get_markets(limit=200)
        except Exception as exc:
            print(f"{name}: API_ERROR fetching markets ({type(exc).__name__}: {exc}), skipping this provider")
            continue
        events = []
        for market in candidates:
            parsed = provider.parse_game_event(market)
            pe = provider_event_from_market(name, market, parsed)
            if pe is not None:
                events.append(pe)
        provider_events[name] = events

    db_conn = get_connection(config.database_path)

    any_qualified = False
    any_evaluated = False

    try:
        for row in rows:
            rec_event = build_recommendation_event(row)
            if rec_event is None:
                continue  # not a game-level market_type -- not in scope for execution analysis

            signal = build_execution_signal(row, rec_event)
            if signal is None:
                # The execution layer must never substitute displayed
                # EV or sportsbook implied probability for a missing
                # model probability -- reject explicitly instead.
                rejection = reject_without_signal(
                    rec_event, "n/a", RejectionReason.MODEL_PROBABILITY_UNAVAILABLE,
                    "historical_recommendations.fair_prob is missing for this recommendation",
                )
                persist_rejection(db_conn, rejection)
                if verbose:
                    print(f"{rec_event.away_team} @ {rec_event.home_team}: "
                          f"REJECTED — {rejection.reason.value}")
                continue

            matches = {}
            for name, events in provider_events.items():
                match = find_best_match(rec_event, events, config.min_market_match_confidence)
                if match is not None:
                    matches[name] = match
                elif verbose:
                    rejection = reject_without_signal(
                        rec_event, name, RejectionReason.NO_PROVIDER_MARKET,
                        "no candidate market on this provider matched with sufficient confidence",
                    )
                    persist_rejection(db_conn, rejection)

            if not matches:
                if verbose:
                    print(f"{signal.away_team} @ {signal.home_team} ({signal.market_type}): "
                          f"NO MATCH on any enabled provider")
                continue

            any_evaluated = True
            comparison = evaluator.compare(signal, matches, providers)
            _persist_comparison_results(db_conn, comparison)

            print("=" * 50)
            print(f"{signal.league} — {signal.away_team} @ {signal.home_team}")
            print(f"Model: {float(signal.model_probability) * 100:.1f}%")
            print(f"Analysis stake: ${config.execution_analysis_stake_usd}")

            if comparison.best is not None:
                _print_opportunity(comparison.best)
            for alt in comparison.qualified_alternatives:
                _print_opportunity(alt)
            for rejection in comparison.provider_rejections:
                if verbose:
                    print(f"{rejection.provider.upper()}: REJECTED — {rejection.reason.value} ({rejection.detail})")
                else:
                    print(f"{rejection.provider.upper()}: REJECTED — {rejection.reason.value}")

            if comparison.best is not None:
                any_qualified = True
                print(f"BEST: {comparison.best.provider.upper()}")
                print("STATUS: QUALIFIED")
            else:
                print("STATUS: NOT QUALIFIED")
    finally:
        db_conn.close()

    if not any_evaluated:
        print("No game-level actionable recommendations matched any enabled provider.")
    elif not any_qualified:
        print("No opportunities currently qualify.")

    # This is a read-only analysis report, not a pass/fail check --
    # finding zero qualified opportunities is a normal, successful run.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.execution.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check-connectivity", help="Health-check every enabled provider (read-only)")

    inspect_parser = subparsers.add_parser(
        "inspect-markets", help="Print parsed market metadata for a provider (read-only)"
    )
    inspect_parser.add_argument("--provider", required=True, choices=list(_ALL_PROVIDERS))
    inspect_parser.add_argument("--limit", type=int, default=20)
    inspect_parser.add_argument("--raw", action="store_true", help="Also print sanitized raw provider JSON")

    scan_parser = subparsers.add_parser(
        "scan-opportunities", help="Evaluate actionable recommendations for executable opportunities (read-only)"
    )
    scan_parser.add_argument("--verbose", action="store_true", help="Print the rejection reason for every non-qualified provider")
    scan_parser.add_argument("--limit", type=int, default=None, help="Only scan the first N actionable recommendations")
    scan_parser.add_argument("--league", default=None, help="Only scan recommendations for this league (e.g. MLB)")
    scan_parser.add_argument(
        "--provider", dest="providers", action="append", choices=list(_ALL_PROVIDERS),
        help="Restrict to this provider (repeatable); default is all enabled providers",
    )
    scan_parser.add_argument("--analysis-stake", type=float, default=None, help="Override execution_analysis_stake_usd for this run")

    inventory_parser = subparsers.add_parser(
        "inventory-report", help="Per-provider market-type/sport/parser-success inventory counts (read-only)"
    )
    inventory_parser.add_argument(
        "--provider", dest="providers", action="append", choices=list(_ALL_PROVIDERS),
        help="Restrict to this provider (repeatable); default is all providers",
    )

    args = parser.parse_args(argv)
    if args.command == "check-connectivity":
        return _check_connectivity()
    if args.command == "inspect-markets":
        return _inspect_markets(args.provider, args.limit, args.raw)
    if args.command == "scan-opportunities":
        return _scan_opportunities(args.verbose, args.limit, args.league, args.providers, args.analysis_stake)
    if args.command == "inventory-report":
        return _inventory_report(args.providers)
    return 1


if __name__ == "__main__":
    sys.exit(main())
