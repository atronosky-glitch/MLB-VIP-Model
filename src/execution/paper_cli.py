"""Stage 3 paper-trading CLI commands.

    python -m src.execution.cli paper-scan [--verbose] [--limit N] [--league MLB]
        [--provider kalshi] [--provider polymarket_us] [--analysis-stake 10]
    python -m src.execution.cli paper-portfolio
    python -m src.execution.cli paper-stats [--today | --days N]
    python -m src.execution.cli settle-paper

All subcommands are simulated only. None ever calls place_order/
cancel_order/modify_order or any write endpoint on either provider --
see tests/test_paper_cli.py's safety regression sweep.

The opportunity-gathering loop in _gather_qualified_signals deliberately
mirrors (rather than reuses/refactors) src.execution.cli._scan_opportunities's
matching/evaluation loop -- Stage 2B's cli.py is not modified by this
stage beyond registering these subparsers, per the explicit instruction
not to rebuild Stage 1/2 functionality. Everything it calls into
(_load_actionable_rows, build_recommendation_event, build_execution_signal,
find_best_match, OpportunityEvaluator.compare) IS reused directly,
unmodified.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from database.db_manager import get_connection
from src.execution import get_provider
from src.execution.cli import _ALL_PROVIDERS, _enabled_providers, _load_actionable_rows
from src.execution.evaluator import OpportunityEvaluator, build_execution_signal
from src.execution.matching import build_recommendation_event, find_best_match, provider_event_from_market
from src.execution.opportunity_store import persist_opportunity, persist_rejection
from src.execution.paper import portfolio, store
from src.execution.paper.broker import PaperBroker
from src.production_config import load_config


def _gather_qualified_signals(
    config: Any, providers: dict, rows: list[dict], league: str | None, limit: int | None,
) -> list[tuple]:
    """Returns a list of (kind, rec_event, signal, comparison) tuples,
    kind in {"no_signal", "no_match", "evaluated"}. Never silently drops
    a row -- every actionable recommendation ends up in exactly one
    bucket, mirroring Stage 2B's own "never silently skip" convention."""
    if league:
        rows = [r for r in rows if (r.get("league") or "").upper() == league.upper()]
    if limit is not None:
        rows = rows[:limit]

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

    evaluator = OpportunityEvaluator(config)
    results: list[tuple] = []
    for row in rows:
        rec_event = build_recommendation_event(row)
        if rec_event is None:
            continue  # not a game-level market_type -- not in scope, same as Stage 2B's scan

        signal = build_execution_signal(row, rec_event)
        if signal is None:
            results.append(("no_signal", rec_event, None, None))
            continue

        matches = {}
        for name, events in provider_events.items():
            match = find_best_match(rec_event, events, config.min_market_match_confidence)
            if match is not None:
                matches[name] = match

        if not matches:
            results.append(("no_match", rec_event, signal, None))
            continue

        comparison = evaluator.compare(signal, matches, providers)
        results.append(("evaluated", rec_event, signal, comparison))

    return results


def paper_scan(
    verbose: bool, limit: int | None, league: str | None,
    only_providers: list[str] | None, analysis_stake: float | None,
) -> int:
    config = load_config()
    if not config.paper_trading_enabled:
        print("Paper trading is disabled (PAPER_TRADING_ENABLED=false).")
        return 0
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
    gathered = _gather_qualified_signals(config, providers, rows, league, limit)

    db_conn = get_connection(config.database_path)
    any_trade = False
    try:
        broker = PaperBroker(db_conn, config)
        for kind, rec_event, signal, comparison in gathered:
            if kind == "no_signal":
                if verbose:
                    print(f"{rec_event.away_team} @ {rec_event.home_team}: SKIPPED (no model probability)")
                continue
            if kind == "no_match":
                if verbose:
                    print(f"{signal.away_team} @ {signal.home_team}: SKIPPED (no provider market match)")
                continue

            # Persist every Stage 2B evaluation, exactly like scan-opportunities does.
            if comparison.best is not None:
                persist_opportunity(db_conn, comparison.best)
            for alt in comparison.qualified_alternatives:
                persist_opportunity(db_conn, alt)
            for rejection in comparison.provider_rejections:
                persist_rejection(db_conn, rejection)
                if verbose:
                    print(f"{rejection.provider.upper()}: REJECTED (Stage 2) — {rejection.reason.value}")

            if comparison.best is None:
                continue  # nothing qualified anywhere -- Stage 2B already recorded why

            opportunity = comparison.best
            provider = providers[opportunity.provider]
            print("=" * 50)
            print(f"{signal.league} — {signal.away_team} vs {signal.home_team}")
            print(f"Provider: {opportunity.provider.upper()}")
            print(f"Net EV: {opportunity.net_ev_pct:+.2f}%")

            result = broker.submit_opportunity(opportunity, signal, provider)
            any_trade = True
            print(f"Sizing mode: {config.bet_sizing_mode}")
            if result.sizing is not None:
                print(f"Recommended: {result.sizing.recommended_units}u / ${result.sizing.recommended_stake_usd}")
            if result.risk_decision is not None:
                print(
                    f"Risk approved: {result.risk_decision.approved_units}u / "
                    f"${result.risk_decision.approved_stake_usd}"
                )
                if result.risk_decision.limiting_constraint:
                    print(f"Limiting factor: {result.risk_decision.limiting_constraint.value}")
            print(f"STATUS: {result.status}")
            if result.rejection_reason:
                print(f"Reason: {result.rejection_reason} -- {result.detail}")
            else:
                print(f"Detail: {result.detail}")
    finally:
        db_conn.close()

    if not any_trade:
        print("No qualified opportunities to paper-trade this run.")
    return 0


def paper_portfolio() -> int:
    config = load_config()
    conn = get_connection(config.database_path)
    try:
        store.get_or_create_account(conn, Decimal(str(config.paper_starting_bankroll_usd)))
        bankroll = portfolio.get_bankroll(conn)
        print(f"Starting bankroll: ${bankroll.starting_bankroll}")
        print(f"Cash: ${bankroll.cash}")
        print(f"Open exposure (at cost): ${bankroll.open_position_cost}")
        print(f"Equity: ${bankroll.equity}")
        print(f"Realized P&L: ${bankroll.realized_pnl}")
        print(f"Unrealized P&L: ${bankroll.unrealized_pnl} (open positions marked at cost, not live-repriced)")
        print(f"Available bankroll: ${bankroll.available_bankroll}")

        positions = store.get_open_positions(conn)
        print(f"\nOpen positions: {len(positions)}")
        for p in positions:
            print(
                f"  {p['provider'].upper()} {p['side']} qty={p['quantity']} "
                f"entry_cost=${p['entry_cost']:.2f} event={p['event_id']}"
            )

        by_provider: dict[str, float] = {}
        by_league: dict[str, float] = {}
        for p in positions:
            by_provider[p["provider"]] = by_provider.get(p["provider"], 0.0) + (p["entry_cost"] or 0.0)
            by_league[p["league"] or "unknown"] = by_league.get(p["league"] or "unknown", 0.0) + (p["entry_cost"] or 0.0)

        print("\nProvider exposure:")
        for name, total in by_provider.items():
            print(f"  {name}: ${total:.2f}")
        print("Sport/league exposure:")
        for name, total in by_league.items():
            print(f"  {name}: ${total:.2f}")
    finally:
        conn.close()
    return 0


def paper_stats(today: bool, days: int | None) -> int:
    config = load_config()
    conn = get_connection(config.database_path)
    try:
        unit_size = Decimal(str(config.unit_size_usd))
        if today:
            stats = portfolio.compute_daily_stats(conn, unit_size)
        else:
            stats = portfolio.compute_stats_range(conn, unit_size, days=days or 7)

        print(f"Period: {stats['start_date']} to {stats['end_date']}")
        print(f"Trades: {stats['trades']} (W {stats['wins']} / L {stats['losses']} / V {stats['voids']} / Pending {stats['pending']})")
        print(f"Amount wagered: ${stats['amount_risked']}")
        print(f"Gross payout: ${stats['gross_payout']}")
        print(f"Fees: ${stats['fees']}")
        print(f"Realized P&L: ${stats['realized_pnl']}")
        print(f"Units won/lost: {stats['units_won_lost']}u")
        print(f"ROI: {stats['roi_pct']:.2f}%")
        print(f"Avg net EV at entry: {stats['avg_net_ev_at_entry']:.2f}%")
        print(f"Best trade: {stats['best_trade']}")
        print(f"Worst trade: {stats['worst_trade']}")
        print(f"Kalshi P&L: ${stats['kalshi_pnl']}")
        print(f"Polymarket US P&L: ${stats['polymarket_us_pnl']}")
    finally:
        conn.close()
    return 0


def settle_paper() -> int:
    config = load_config()
    conn = get_connection(config.database_path)
    try:
        broker = PaperBroker(conn, config)
        enabled = _enabled_providers(config)
        providers: dict[str, Any] = {}
        for name in _ALL_PROVIDERS:
            if not enabled.get(name):
                continue
            try:
                providers[name] = get_provider(name, config)
            except Exception as exc:
                print(f"{name}: could not initialize ({type(exc).__name__}: {exc}), skipping settlement for this provider")

        if not providers:
            print("No providers enabled -- nothing to settle.")
            return 0

        outcomes = broker.settle_positions(providers)
        if not outcomes:
            print("No open positions to settle.")
            return 0

        for outcome in outcomes:
            pnl_note = f" (P&L: ${outcome.realized_pnl})" if outcome.realized_pnl is not None else ""
            print(f"Position {outcome.position_id}: {outcome.status.value} -- {outcome.detail}{pnl_note}")
    finally:
        conn.close()
    return 0
