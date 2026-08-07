"""Generic player-prop edge scanner.

Replaces the strikeout-specific scanner with a registry-driven pipeline
that supports every market in the MarketConfig registry.

Usage::

    python -m src.player_prop_scanner --market strikeouts
    python -m src.player_prop_scanner --market outs --all
    python -m src.player_prop_scanner --market hits_allowed --market-form ou
    python -m src.player_prop_scanner --market walks_allowed --market-form yn
    python -m src.player_prop_scanner --market earned_runs --positive-only

Market form defaults to ``ou`` when omitted.  Use ``--market-form yn``
for Yes/No single-sided comparison markets.  Combinations that the
registry does not support are rejected with a nonzero exit code.

The old ``python -m src.strikeout_scanner`` command is preserved as a
thin backward-compatible wrapper that delegates here.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import NamedTuple

from . import prop_config as cfg
from .api_client import SportsGameOddsClient
from .player_prop_parser import parse_player_props
from .player_prop_analysis import analyze_prop_group, analyze_yn_group, is_pinnacle_book
from .pinnacle_feed import PinnacleFeedClient, build_pinnacle_lookup, inject_pinnacle_reference
from .validation_constants import APPROVED_STATUSES
from database.db_manager import get_connection, create_run, finish_run

logger = logging.getLogger(__name__)

# Market-quality ranks for sorting (lower = higher quality)
_MQ_RANK = {
    cfg.MARKET_QUALITY_VALID: 0,
    cfg.MARKET_QUALITY_NEEDS_REVIEW: 1,
    cfg.MARKET_QUALITY_INSUFFICIENT: 2,
}


# ==================================================================
# Pinnacle diagnostics (logging only — never changes pick logic)
# ==================================================================

def _new_pinnacle_summary() -> dict:
    """Counter block for the end-of-analysis Pinnacle diagnostics summary."""
    return {
        "total_groups": 0,
        "pinnacle_exact_match": 0,
        "pinnacle_reference_used": 0,
        "pinnacle_missing": 0,
        "pinnacle_line_mismatch": 0,
        "pinnacle_one_side": 0,
        "pinnacle_model_disabled": 0,
        "insufficient_comparison_books": 0,
        "ev_threshold_failed": 0,
        "prob_edge_threshold_failed": 0,
        "no_positive_edge": 0,
        "fallback_lean": 0,
        "official_approved": 0,
    }


def _accumulate_pinnacle_summary(summary: dict, analysis: dict) -> None:
    """Fold one group analysis result into the compact summary counters.

    ``fallback_lean`` counts groups that used the LOO median fallback and
    reached the lean stage; groups rejected for insufficient comparison
    books are excluded (they never produced a lean).
    """
    diag = analysis.get("diagnostics") or {}
    summary["total_groups"] += 1
    if diag.get("pinnacle_both_sides"):
        summary["pinnacle_exact_match"] += 1
    if diag.get("pinnacle_reference_used"):
        summary["pinnacle_reference_used"] += 1
    if (diag.get("fallback_used")
            and diag.get("rejection_reason") != "insufficient_comparison_books"):
        summary["fallback_lean"] += 1
    if diag.get("official_approved"):
        summary["official_approved"] += 1
    reason_key = {
        "missing_pinnacle": "pinnacle_missing",
        "pinnacle_line_mismatch": "pinnacle_line_mismatch",
        "pinnacle_missing_opposite_side": "pinnacle_one_side",
        "pinnacle_model_disabled": "pinnacle_model_disabled",
        "insufficient_comparison_books": "insufficient_comparison_books",
        "ev_threshold_failed": "ev_threshold_failed",
        "prob_edge_threshold_failed": "prob_edge_threshold_failed",
        "no_positive_edge": "no_positive_edge",
    }.get(diag.get("rejection_reason", ""))
    if reason_key:
        summary[reason_key] += 1


def _log_pinnacle_summary(summary: dict) -> None:
    """Emit one compact INFO line summarising the whole O/U analysis run."""
    logger.info(
        "PINNACLE_SUMMARY total_groups=%d exact_match=%d reference_used=%d "
        "pinnacle_missing=%d line_mismatch=%d one_side=%d model_disabled=%d "
        "insufficient_books=%d ev_threshold_failed=%d "
        "prob_edge_threshold_failed=%d no_positive_edge=%d fallback_lean=%d "
        "official_approved=%d",
        summary.get("total_groups", 0),
        summary.get("pinnacle_exact_match", 0),
        summary.get("pinnacle_reference_used", 0),
        summary.get("pinnacle_missing", 0),
        summary.get("pinnacle_line_mismatch", 0),
        summary.get("pinnacle_one_side", 0),
        summary.get("pinnacle_model_disabled", 0),
        summary.get("insufficient_comparison_books", 0),
        summary.get("ev_threshold_failed", 0),
        summary.get("prob_edge_threshold_failed", 0),
        summary.get("no_positive_edge", 0),
        summary.get("fallback_lean", 0),
        summary.get("official_approved", 0),
    )


def _log_line_fragmentation(ou_groups: dict) -> None:
    """DEBUG-log the exact lines available per player+market.

    For each ``(player_id, market_type)`` pair, summarise every exact line that
    appears: line value, number of books, book names, whether Pinnacle is on
    that exact line, whether Pinnacle exists on a different line, and the
    Over/Under side counts.
    """
    pm_lines: dict[tuple[str, str], dict] = {}
    for gd in ou_groups.values():
        pm_key = (gd.get("player_id", ""), gd.get("market_type", ""))
        line = gd.get("line")
        books = sorted(set(gd["over"]) | set(gd["under"]))
        line_entry = pm_lines.setdefault(pm_key, {}).setdefault(line, {
            "books": [],
            "pinnacle": False,
            "over_books": 0,
            "under_books": 0,
        })
        for b in books:
            if b not in line_entry["books"]:
                line_entry["books"].append(b)
        line_entry["books"].sort()
        if any(is_pinnacle_book(b) for b in books):
            line_entry["pinnacle"] = True
        line_entry["over_books"] = len(gd["over"])
        line_entry["under_books"] = len(gd["under"])

    for (player_id, market_type), lines in sorted(pm_lines.items()):
        pinnacle_lines = {l for l, e in lines.items() if e["pinnacle"]}
        lines_sorted = sorted(lines.keys(), key=lambda x: (x is None, str(x)))
        for line in lines_sorted:
            e = lines[line]
            other_pinnacle = sorted(l for l in pinnacle_lines if l != line)
            logger.debug(
                "LINE_FRAGMENTATION player=%s market=%s line=%s books=%d "
                "book_names=%s pinnacle_on_line=%s pinnacle_other_lines=%s "
                "over_books=%d under_books=%d",
                player_id, market_type, line, len(e["books"]),
                ",".join(e["books"]), bool(e["pinnacle"]),
                ",".join(str(l) for l in other_pinnacle),
                e["over_books"], e["under_books"],
            )


# ==================================================================
# Market / form resolution
# ==================================================================

class ResolvedMarkets(NamedTuple):
    market_names: list[str]       # ["strikeouts"] or ["outs", "hits_allowed", ...]
    form: str                     # "ou", "yn", or "all"
    market_configs: list[cfg.MarketConfig]


def resolve_markets(market: str, form: str) -> ResolvedMarkets:
    """Validate and resolve market + form into a list of MarketConfig objects.

    Parameters
    ----------
    market : str
        CLI market name (``"strikeouts"``, ``"outs"``, ``"hits_allowed"``,
        ``"walks_allowed"``, ``"earned_runs"``) or ``"all"``.
    form : str
        ``"ou"``, ``"yn"``, or ``"all"``.

    Returns
    -------
    ResolvedMarkets namedtuple.

    Raises
    ------
    SystemExit
        If market name is invalid or the combination is unsupported.
    """
    valid_cli = [m.cli_name for m in cfg.MARKET_REGISTRY]
    valid_forms = ("ou", "yn", "all")

    if market not in valid_cli and market != "all":
        print(f"ERROR: Invalid market '{market}'. Valid markets: {', '.join(valid_cli)}, all",
              file=sys.stderr)
        sys.exit(1)
    if form not in valid_forms:
        print(f"ERROR: Invalid form '{form}'. Valid forms: {', '.join(valid_forms)}",
              file=sys.stderr)
        sys.exit(1)

    if market == "all":
        configs = list(cfg.MARKET_REGISTRY)
    else:
        mc = cfg.get_market_by_cli_name(market)
        if mc is None:
            print(f"ERROR: Unknown market '{market}'", file=sys.stderr)
            sys.exit(1)
        configs = [mc]

    # Validate each requested form is supported
    if market == "all":
        # When requesting all markets, silently filter to supported forms
        configs = [mc for mc in configs
                   if (form == "ou" and mc.supports_ou)
                   or (form == "yn" and mc.supports_yn)
                   or form == "all"]
    else:
        for mc in configs:
            if form == "ou" and not mc.supports_ou:
                valid_forms_for_mc = "yn" if not mc.supports_ou else ("ou, yn" if mc.supports_yn else "ou")
                print(f"ERROR: '{mc.cli_name}' does not support O/U form. "
                      f"Supported forms: {valid_forms_for_mc}", file=sys.stderr)
                sys.exit(1)
            if form == "yn" and not mc.supports_yn:
                valid_forms_for_mc = "ou" if not mc.supports_yn else ("ou, yn" if mc.supports_ou else "yn")
                print(f"ERROR: '{mc.cli_name}' does not support YN form. "
                      f"Supported forms: {valid_forms_for_mc}", file=sys.stderr)
                sys.exit(1)

    return ResolvedMarkets(
        market_names=[mc.cli_name for mc in configs],
        form=form,
        market_configs=configs,
    )


def _accepted_market_types(resolved: ResolvedMarkets) -> set[str]:
    """Return the set of market_type strings that match the resolution."""
    types: set[str] = set()
    for mc in resolved.market_configs:
        if resolved.form in ("ou", "all") and mc.supports_ou:
            types.add(mc.market_type_ou)
        if resolved.form in ("yn", "all") and mc.supports_yn:
            types.add(mc.market_type_yn)
    return types


# ==================================================================
# Core scan
# ==================================================================

def _group_side(side: str, market_type: str) -> str:
    """Map registry-defined game sides into the generic two-sided slots."""
    market = cfg.get_market_by_ou_type(market_type)
    mapping = getattr(market, "internal_side_map", None) if market else None
    return (mapping or {}).get(side, side)


def run_scan(
    mode: str = "actionable",
    min_ev: float | None = None,
    limit: int = 25,
    market: str = "all",
    market_form: str = "all",
    sportsbook: str | None = None,
    player: str | None = None,
    game: str | None = None,
) -> dict:
    """Run the full generic scanner pipeline.

    Parameters
    ----------
    mode : str
        ``"all"``, ``"positive"``, or ``"actionable"``.
    min_ev : float or None
        Override actionable EV threshold as decimal (e.g. 0.02 = 2%).
        Only applied to O/U markets.  Ignored (with warning) for YN.
    limit : int
        Maximum number of opportunities to return per form.
    market : str
        Market CLI name (``"strikeouts"``, ``"outs"``, ``"all"``).
    market_form : str
        ``"ou"``, ``"yn"``, or ``"all"`` (default ``"all"``).
    sportsbook : str or None
        Case-insensitive filter.  Only show rows from this sportsbook.
    player : str or None
        Case-insensitive filter.  Only show rows matching this player name.
    game : str or None
        Case-insensitive filter.  Only show rows from events matching
        this substring (team name or event ID).

    Returns
    -------
    dict with keys:
        opportunities, yn_opportunities, n_events, n_markets, n_pitchers,
        scan_start, fetch_time, data_source, oldest_obs, newest_obs,
        age_seconds, stale_warning, research_only, scanner_title,
        n_approved_rows, n_excluded_rows.
    """
    resolved = resolve_markets(market, market_form)
    accepted_types = _accepted_market_types(resolved)

    scan_start = datetime.now(timezone.utc)

    # Create run record for auditability
    _run_id: str | None = None
    try:
        _conn = get_connection()
        try:
            _run_id = create_run(
                _conn, run_type="scan", mode=mode,
                market_filter=market, form_filter=market_form,
            )
        finally:
            _conn.close()
    except Exception:
        logger.debug("Could not create run record (DB may be unavailable)")

    client = SportsGameOddsClient(max_cache_age=cfg.LIVE_CACHE_TTL_SECONDS)

    logger.info("Fetching MLB events...")
    data, from_cache = client.get_events(
        league="MLB", odds_available=True, include_alt_lines=True,
    )
    data_source = "CACHE" if from_cache else "LIVE API"
    fetch_time = datetime.now(timezone.utc)

    events = data.get("data", data.get("events", [])) or []

    # Parse all events
    all_odds: list[dict] = []
    all_audit: list[dict] = []
    for event in events:
        parsed = parse_player_props(event)
        all_odds.extend(parsed.odds_rows)
        all_audit.extend(parsed.audit_rows)

    if not all_odds:
        return _empty_result(events, scan_start, fetch_time, data_source, from_cache)

    # Observation timestamps
    obs_times: list[datetime] = []
    for r in all_odds:
        if r.get("observation_time"):
            try:
                obs_times.append(datetime.fromisoformat(r["observation_time"]))
            except (ValueError, TypeError):
                pass

    newest_obs = max(obs_times) if obs_times else fetch_time
    oldest_obs = min(obs_times) if obs_times else fetch_time
    age_seconds = round((fetch_time - newest_obs).total_seconds()) if obs_times else 0
    stale_warning = age_seconds > cfg.FRESHNESS_THRESHOLD_SECONDS if obs_times else False

    # Research-only: cached data OR all games have started
    research_only = from_cache
    if not research_only:
        now = datetime.now(timezone.utc)
        for ev in events:
            status_obj = ev.get("status", {}) or {}
            starts_at = (status_obj.get("startsAt") if isinstance(status_obj, dict)
                         else ev.get("startDate") or "")
            if starts_at:
                try:
                    dt = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
                    if dt > now:
                        research_only = False
                        break
                except (ValueError, TypeError):
                    pass
        else:
            if events:
                research_only = True

    # Event lookup for game info
    event_map = _build_event_map(events)

    # Group approved rows by market_group_key, separating O/U and YN
    ou_groups: dict[str, dict] = {}
    yn_groups: dict[str, dict] = {}
    excluded_count = 0
    approved_count = 0
    seen_books: set[str] = set()

    for row in all_odds:
        if row["validation_status"] not in APPROVED_STATUSES:
            excluded_count += 1
            continue
        if row["market_type"] not in accepted_types:
            excluded_count += 1
            continue

        approved_count += 1
        seen_books.add(row["sportsbook"])
        key = row["market_group_key"]
        market_type = row["market_type"]

        if cfg.get_market_by_yn_type(market_type) is not None:
            if key not in yn_groups:
                yn_groups[key] = {"yes": {}, "player_id": row["player_id"],
                                  "player_name": row["player_name"],
                                  "event_id": row["event_id"],
                                  "market_type": market_type,
                                  "observation_times": []}
            if row.get("observation_time"):
                yn_groups[key]["observation_times"].append(row["observation_time"])
            side = row["side"]
            if side == "YES":
                yn_groups[key]["yes"][row["sportsbook"]] = {
                    "price": row["price"],
                    "decimal_odds": row["decimal_odds"],
                    "validation_status": row["validation_status"],
                }
        elif cfg.get_market_by_ou_type(market_type) is not None:
            if key not in ou_groups:
                ou_groups[key] = {"over": {}, "under": {}, "line": row["line"],
                                  "player_id": row["player_id"],
                                  "player_name": row["player_name"],
                                  "event_id": row["event_id"],
                                   "market_type": market_type,
                                   "observation_times": [],
                                   "display_sides": {}}
            if row.get("observation_time"):
                ou_groups[key]["observation_times"].append(row["observation_time"])
            source_side = row["side"]
            side = _group_side(source_side, market_type)
            ou_groups[key]["display_sides"][side.lower()] = source_side
            ou_groups[key][side.lower()][row["sportsbook"]] = {
                "price": row["price"],
                "decimal_odds": row["decimal_odds"],
                "line": row["line"],
                "validation_status": row["validation_status"],
                "display_side": source_side,
            }

    # Analyze each O/U group
    if seen_books:
        print(f"  Books in approved O/U+YN rows ({len(seen_books)}): {', '.join(sorted(seen_books))}")
    if ou_groups:
        print(f"  O/U groups formed: {len(ou_groups)}")
        for gk, gd in ou_groups.items():
            has_both = "BOTH" if gd["over"] and gd["under"] else "MISSING_SIDE"
            line_display = "?" if gd.get("line") is None else str(gd.get("line"))
            print(f"    [{has_both}] {gd['market_type']:35} line={line_display:>6}  "
                  f"over_books={len(gd['over'])} under_books={len(gd['under'])}  "
                  f"player={gd.get('player_name','?')[:20]}")
    _log_line_fragmentation(ou_groups)

    # Inject Pinnacle reference prices into O/U groups so the frozen
    # Pinnacle value model can compute a no-vig reference.  The feed
    # client reuses a fresh disk cache (5-min TTL) and rate-limits live
    # calls, so allow_fetch=True is safe for every scan: a fresh cache
    # never touches the network, and a stale cache refetches sharp prices
    # even for cached/research scans (otherwise those runs silently have
    # no Pinnacle reference at all).
    pinnacle_reference_injected = 0
    if cfg.PINNACLE_FEED_ENABLED:
        try:
            _pinnacle_props = PinnacleFeedClient().get_mlb_props(allow_fetch=True)
        except Exception as exc:  # noqa: BLE001 - a dead feed must never block a scan
            logger.warning("Pinnacle feed unavailable: %s", exc)
            _pinnacle_props = None
        if _pinnacle_props:
            logger.info("PINNACLE_FEED_PROPS parsed=%d", len(_pinnacle_props))
            _pinnacle_lookup = build_pinnacle_lookup(_pinnacle_props)
            pinnacle_reference_injected = inject_pinnacle_reference(
                ou_groups, event_map, _pinnacle_lookup
            )
            if pinnacle_reference_injected:
                print(
                    f"  Pinnacle reference injected into {pinnacle_reference_injected} "
                    f"O/U groups"
                )
        else:
            logger.warning("PINNACLE_FEED_PROPS parsed=0; no Pinnacle references available")

    pinnacle_summary = _new_pinnacle_summary()
    opportunities = []
    for gkey, gdata in ou_groups.items():
        analysis = analyze_prop_group(
            gkey, gdata["over"], gdata["under"],
            n_excluded_rows=excluded_count,
            n_approved_rows=approved_count,
        )
        _accumulate_pinnacle_summary(pinnacle_summary, analysis)
        if analysis["market_quality"] == cfg.MARKET_QUALITY_EXCLUDED:
            continue

        mq = analysis["market_quality"]
        is_rec_eligible = (mq == cfg.MARKET_QUALITY_VALID)
        ev_info = event_map.get(gdata["event_id"], {})

        for book_entry in analysis["books"]:
            if not book_entry["included"]:
                continue

            bet_status = book_entry["bet_status"]
            ev_pct = book_entry["ev_pct"]

            # Mode filtering
            if mode == "positive":
                if not is_rec_eligible:
                    continue
                if bet_status not in (cfg.BET_STATUS_STRONG, cfg.BET_STATUS_POSITIVE,
                                      cfg.BET_STATUS_MARGINAL):
                    continue
            elif mode == "actionable":
                if not is_rec_eligible:
                    continue
                threshold = min_ev if min_ev is not None else cfg.ACTIONABLE_EDGE_THRESHOLD
                if ev_pct < threshold * 100:
                    continue

            opp = {
                "event_id": gdata["event_id"],
                "away_team": ev_info.get("away_name", ""),
                "home_team": ev_info.get("home_name", ""),
                "start_time": ev_info.get("start_time", ""),
                "player_id": gdata["player_id"],
                "player_name": gdata["player_name"],
                "market_type": gdata["market_type"],
                "line": gdata["line"],
                "side": gdata.get("display_sides", {}).get(
                    book_entry["side"].lower(), book_entry["side"]
                ),
                "sportsbook": book_entry["sportsbook"],
                "american_odds": book_entry["american_odds"],
                "decimal_odds": book_entry["decimal_odds"],
                "n_consensus_books": analysis["n_paired_books"],
                "fair_prob": book_entry["fair_prob"],
                "ev_pct": book_entry["ev_pct"],
                "market_quality": mq,
                "rec_eligible": is_rec_eligible,
                "is_official": bool(book_entry.get("is_official", False)),
                "bet_status": bet_status,
                "validation_status": book_entry.get("validation_status", ""),
                "is_alt_line": 0,
                "pinnacle_approved": book_entry.get("pinnacle_approved"),
                "pinnacle_ev": book_entry.get("pinnacle_ev"),
                "pinnacle_prob_edge": book_entry.get("pinnacle_prob_edge"),
                "pinnacle_fair_prob": book_entry.get("pinnacle_fair_prob"),
                "pinnacle_reference_used": analysis.get("pinnacle_reference_used"),
                "pinnacle_found": analysis.get("pinnacle_found"),
                "pinnacle_book": analysis.get("pinnacle_book"),
                "pinnacle_line": analysis.get("line") if analysis.get("pinnacle_reference_used") else None,
                "pinnacle_over_price": analysis.get("pinnacle_over_price"),
                "pinnacle_under_price": analysis.get("pinnacle_under_price"),
                "observation_time": max(gdata.get("observation_times") or [""]),
            }
            opportunities.append(opp)

    if ou_groups:
        n_ou_opps = len(opportunities)
        print(f"  O/U opportunities from scan: {n_ou_opps}")

    _log_pinnacle_summary(pinnacle_summary)

    # YN groups formed (debug)
    yn_groups_with_yes = sum(1 for g in yn_groups.values() if g["yes"])
    if yn_groups:
        print(f"  YN groups formed: {len(yn_groups)}  (with YES side: {yn_groups_with_yes})")
        market_types = {}
        for gd in yn_groups.values():
            mt = gd.get("market_type", "?")
            market_types[mt] = market_types.get(mt, 0) + 1
        for mt, count in sorted(market_types.items(), key=lambda x: -x[1]):
            print(f"    {mt}: {count} groups")

    # Analyze each YN group
    yn_opportunities = []
    for gkey, gdata in yn_groups.items():
        if not gdata["yes"]:
            continue
        analysis = analyze_yn_group(
            gkey, gdata["yes"],
            n_excluded_rows=excluded_count,
            n_approved_rows=approved_count,
        )
        if analysis["market_quality"] == cfg.MARKET_QUALITY_EXCLUDED:
            continue

        ev_info = event_map.get(gdata["event_id"], {})

        for book_entry in analysis["books"]:
            comparison_status = book_entry["comparison_status"]
            is_rec_eligible = book_entry["recommendation_eligible"]

            # Mode filtering
            if mode == "positive":
                if not is_rec_eligible:
                    continue
            elif mode == "actionable":
                if not is_rec_eligible:
                    continue

            opp = {
                "event_id": gdata["event_id"],
                "away_team": ev_info.get("away_name", ""),
                "home_team": ev_info.get("home_name", ""),
                "start_time": ev_info.get("start_time", ""),
                "player_id": gdata["player_id"],
                "player_name": gdata["player_name"],
                "market_type": gdata["market_type"],
                "line": None,
                "side": "YES",
                "sportsbook": book_entry["sportsbook"],
                "american_odds": book_entry["american_odds"],
                "decimal_odds": book_entry["decimal_odds"],
                "n_consensus_books": analysis["n_books"],
                "price_advantage_pct": book_entry["price_advantage_pct"],
                "relative_payout_advantage_pct": book_entry["relative_payout_advantage_pct"],
                "decimal_odds_advantage": book_entry["decimal_odds_advantage"],
                "market_reference_probability": book_entry["market_reference_probability"],
                "market_reference_odds": book_entry["market_reference_odds"],
                "comparison_status": comparison_status,
                "market_quality": analysis["market_quality"],
                "rec_eligible": is_rec_eligible,
                "validation_status": book_entry.get("validation_status", ""),
                "observation_time": max(gdata.get("observation_times") or [""]),
            }
            yn_opportunities.append(opp)

    # ── Filtering ──
    if sportsbook:
        sb_lower = sportsbook.lower()
        opportunities = [o for o in opportunities if sb_lower in o["sportsbook"].lower()]
        yn_opportunities = [o for o in yn_opportunities if sb_lower in o["sportsbook"].lower()]

    if player:
        pl_lower = player.lower()
        opportunities = [o for o in opportunities if pl_lower in o["player_name"].lower()]
        yn_opportunities = [o for o in yn_opportunities if pl_lower in o["player_name"].lower()]

    if game:
        gm_lower = game.lower()
        def _match_game(opp: dict) -> bool:
            away = opp.get("away_team", "").lower()
            home = opp.get("home_team", "").lower()
            if gm_lower in away or gm_lower in home:
                return True
            # Match abbreviated team names (e.g. "NYY" vs "New York Yankees")
            # by also checking the matchup string "away @ home"
            matchup = f"{away} @ {home}"
            if gm_lower in matchup:
                return True
            # Event-ID: require at least 4 chars to avoid false positives on
            # short substrings like "1" or "mlb"
            eid = opp.get("event_id", "")
            if len(gm_lower) >= 4 and gm_lower in eid.lower():
                return True
            return False
        opportunities = [o for o in opportunities if _match_game(o)]
        yn_opportunities = [o for o in yn_opportunities if _match_game(o)]

    # ── Sort and deduplicate O/U ──
    opportunities.sort(key=lambda o: (
        -o["ev_pct"],
        _MQ_RANK.get(o["market_quality"], 99),
        -(o["n_consensus_books"] or 0),
        o.get("start_time", ""),
        o["player_name"],
        o["sportsbook"],
    ))
    deduped: dict[tuple, dict] = {}
    for opp in opportunities:
        key = (opp["event_id"], opp["player_id"], opp["line"],
               opp["side"], opp["sportsbook"])
        if key not in deduped:
            deduped[key] = opp
    opportunities = list(deduped.values())
    opportunities.sort(key=lambda o: (
        -o["ev_pct"],
        _MQ_RANK.get(o["market_quality"], 99),
        -(o["n_consensus_books"] or 0),
        o.get("start_time", ""),
        o["player_name"],
        o["sportsbook"],
    ))

    # ── Sort and deduplicate YN ──
    yn_opportunities.sort(key=lambda o: (
        -o.get("price_advantage_pct", 0),
        -(o["n_consensus_books"] or 0),
        o.get("start_time", ""),
        o["player_name"],
        o["sportsbook"],
    ))
    yn_deduped: dict[tuple, dict] = {}
    for opp in yn_opportunities:
        key = (opp["event_id"], opp["player_id"], opp["sportsbook"])
        if key not in yn_deduped:
            yn_deduped[key] = opp
    yn_opportunities = list(yn_deduped.values())
    yn_opportunities.sort(key=lambda o: (
        -o.get("price_advantage_pct", 0),
        -(o["n_consensus_books"] or 0),
        o.get("start_time", ""),
        o["player_name"],
        o["sportsbook"],
    ))

    # ── Limit ──
    if limit:
        opportunities = opportunities[:limit]
        yn_opportunities = yn_opportunities[:limit]

    if yn_opportunities:
        yn_market_counts = {}
        for o in yn_opportunities:
            mt = o.get("market_type", "?")
            yn_market_counts[mt] = yn_market_counts.get(mt, 0) + 1
        print(f"  YN opportunities after filtering: {len(yn_opportunities)}")
        for mt, cnt in sorted(yn_market_counts.items(), key=lambda x: -x[1]):
            print(f"    {mt}: {cnt}")

    # ── Determine scanner title ──
    scanner_title = _build_scanner_title(resolved)

    result = {
        "opportunities": opportunities,
        "yn_opportunities": yn_opportunities,
        "n_events": len(events),
        "n_markets": len(ou_groups) + len(yn_groups),
        "n_pitchers": len({g["player_id"] for g in ou_groups.values()} |
                          {g["player_id"] for g in yn_groups.values()}),
        "n_approved_rows": approved_count,
        "n_excluded_rows": excluded_count,
        "scan_start": scan_start.isoformat(),
        "fetch_time": fetch_time.isoformat(),
        "data_source": data_source,
        "oldest_obs": oldest_obs.isoformat() if isinstance(oldest_obs, datetime) else "",
        "newest_obs": newest_obs.isoformat() if isinstance(newest_obs, datetime) else "",
        "age_seconds": age_seconds,
        "stale_warning": stale_warning,
        "research_only": research_only,
        "scanner_title": scanner_title,
        "run_id": _run_id or "",
        "pinnacle_diagnostics": pinnacle_summary,
    }

    # Finish run record
    if _run_id:
        try:
            _conn = get_connection()
            try:
                finish_run(
                    _conn, _run_id,
                    n_events=result["n_events"],
                    n_markets=result["n_markets"],
                    n_opportunities=len(opportunities),
                    n_yn_opps=len(yn_opportunities),
                    data_source=data_source,
                    research_only=research_only,
                )
            finally:
                _conn.close()
        except Exception:
            logger.debug("Could not finish run record")

    return result


def _empty_result(events, scan_start, fetch_time, data_source, from_cache):
    """Return an empty result dict."""
    return {
        "opportunities": [],
        "yn_opportunities": [],
        "n_events": len(events),
        "n_markets": 0,
        "n_pitchers": 0,
        "n_approved_rows": 0,
        "n_excluded_rows": 0,
        "scan_start": scan_start.isoformat(),
        "fetch_time": fetch_time.isoformat(),
        "data_source": data_source,
        "oldest_obs": "",
        "newest_obs": "",
        "age_seconds": 0,
        "stale_warning": False,
        "research_only": from_cache,
        "scanner_title": "MLB PLAYER PROP EDGE SCANNER",
        "run_id": "",
        "pinnacle_diagnostics": _new_pinnacle_summary(),
    }


def _build_event_map(events: list[dict]) -> dict:
    """Build event_id → game info lookup."""
    event_map = {}
    for ev in events:
        eid = ev.get("eventID") or ev.get("id")
        if eid:
            teams = ev.get("teams", {}) or {}
            home = teams.get("home", {}) or {}
            away = teams.get("away", {}) or {}
            status_obj = ev.get("status", {}) or {}
            event_map[eid] = {
                "away_name": away.get("names", {}).get("long") or away.get("name") or "?",
                "home_name": home.get("names", {}).get("long") or home.get("name") or "?",
                "start_time": (status_obj.get("startsAt") if isinstance(status_obj, dict)
                               else ev.get("startDate") or ""),
            }
    return event_map


def _build_scanner_title(resolved: ResolvedMarkets) -> str:
    """Build the scanner title from resolved markets."""
    if len(resolved.market_configs) == 1:
        return resolved.market_configs[0].scanner_title or "MLB PLAYER PROP EDGE SCANNER"
    return "MLB PLAYER PROP EDGE SCANNER"


# ==================================================================
# Display
# ==================================================================

def _fmt_odds(odds: int) -> str:
    return f"+{odds}" if odds > 0 else str(odds)


def _fmt_ev(ev_pct: float) -> str:
    return f"{ev_pct:+.2f}%"


def display_results(result: dict, mode: str) -> None:
    """Print scanner results to terminal."""
    opps = result["opportunities"]
    yn_opps = result.get("yn_opportunities", [])
    title = result.get("scanner_title", "MLB PLAYER PROP EDGE SCANNER")

    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)

    mode_label = {
        "all": "ALL MARKETS",
        "positive": "RECOMMENDATIONS — POSITIVE EV",
        "actionable": "RECOMMENDATIONS — ACTIONABLE EDGE",
    }.get(mode, mode.upper())
    print(f"  Mode: {mode_label}")
    print(f"  Source: {result['data_source']}")
    print(f"  Scan started: {result['scan_start'][:19].replace('T', ' ')} UTC")
    print(f"  Data fetched:  {result['fetch_time'][:19].replace('T', ' ')} UTC")
    if result.get("newest_obs"):
        print(f"  Odds observed: {result['newest_obs'][:19].replace('T', ' ')} UTC  "
              f"(age: {result['age_seconds']}s)")
    else:
        print("  Freshness: UNKNOWN (no observation timestamps)")
    if result["stale_warning"]:
        print(f"  ** WARNING: Data is older than "
              f"{cfg.FRESHNESS_THRESHOLD_SECONDS}s freshness threshold **")
    if result["research_only"]:
        print("  ** RESEARCH ONLY — data is from cache or all games have started. **")
        print("  ** These results are for historical/analytical purposes only, not   **")
        print("  ** current betting decisions.                                       **")

    print(f"  Events: {result['n_events']}  |  Markets: {result['n_markets']}  "
          f"|  Pitchers: {result['n_pitchers']}")
    print(f"  Odds: {result.get('n_approved_rows', 0)} approved, "
          f"{result.get('n_excluded_rows', 0)} excluded")

    # ── O/U Results ──
    if opps:
        print()
        print("  OVER/UNDER — TRUE EV ANALYSIS")
        print(f"  Showing {len(opps)} result(s)")
        print()
        header = (
            f"  {'':>3} {'Pitcher':<20} {'Side':<6} {'Line':>5} {'Book':<14} "
            f"{'Odds':>7} {'EV%':>8} {'Pin':>3} {'MQ':<18} {'Rec':>4}"
        )
        print(header)
        print("  " + "-" * 100)

        for i, opp in enumerate(opps, 1):
            pitcher = opp["player_name"][:18] if opp["player_name"] else opp["player_id"][:18]
            side = opp["side"]
            line = f"{opp['line']:.1f}" if opp["line"] is not None else "N/A"
            book = opp["sportsbook"][:12]
            odds = _fmt_odds(opp["american_odds"])
            ev = _fmt_ev(opp["ev_pct"])
            pin = "Y" if opp.get("pinnacle_approved") else ("N" if opp.get("pinnacle_approved") is not None else "-")
            mq = opp["market_quality"][:16]
            rec = "YES" if opp.get("rec_eligible") else "NO"
            print(f"  {i:>3} {pitcher:<20} {side:<6} {line:>5} {book:<14} "
                  f"{odds:>7} {ev:>8} {pin:>3} {mq:<18} {rec:>4}")
    elif not yn_opps:
        print()
        print("  NO QUALIFYING OPPORTUNITIES")
        if result.get("n_approved_rows", 0) == 0:
            print("  Hint: No approved odds rows were found. Check API connection,")
            print("        data freshness, or validation status of ingested odds.")
        elif result.get("n_markets", 0) == 0:
            print("  Hint: No market groups matched the requested market/form filter.")
            print("        --market all --market-form yn only includes markets that")
            print("        support Yes/No (walks_allowed, earned_runs, strikeouts).")
            print("        Try --market all --market-form all to see all available data.")
        print()
        return

    # ── YN Results ──
    if yn_opps:
        print()
        print("  SINGLE-SIDED MARKET COMPARISON / TRUE EV NOT AVAILABLE")
        print("  Reference: LOO median implied probability")
        print(f"  Showing {len(yn_opps)} result(s)")
        print()
        yn_header = (
            f"  {'':>3} {'Pitcher':<20} {'Book':<14} "
            f"{'Odds':>7} {'Ref Prob':>9} {'Adv%':>7} {'DecAdv':>7} "
            f"{'Status':<24} {'Rec':>4}"
        )
        print(yn_header)
        print("  " + "-" * 100)

        for i, opp in enumerate(yn_opps, 1):
            pitcher = opp["player_name"][:18] if opp["player_name"] else opp["player_id"][:18]
            book = opp["sportsbook"][:12]
            odds = _fmt_odds(opp["american_odds"])
            ref_prob = f"{opp['market_reference_probability']:.1%}"
            adv = f"{opp['price_advantage_pct']:+.2f}%"
            dec_adv = f"{opp['decimal_odds_advantage']:+d}"
            status = opp["comparison_status"][:22]
            rec = "YES" if opp.get("rec_eligible") else "NO"
            print(f"  {i:>3} {pitcher:<20} {book:<14} "
                  f"{odds:>7} {ref_prob:>9} {adv:>7} {dec_adv:>7} {status:<24} {rec:>4}")

    print()


def display_verbose(result: dict) -> None:
    """Print detailed per-opportunity info."""
    opps = result["opportunities"]
    yn_opps = result.get("yn_opportunities", [])
    print()
    for i, opp in enumerate(opps, 1):
        rec_label = "RECOMMENDATION ELIGIBLE" if opp.get("rec_eligible") else "RESEARCH ONLY"
        print(f"  {i}. {opp['player_name']} {opp['side']} {opp['line']}  [{rec_label}]")
        print(f"     Sportsbook:   {opp['sportsbook']}")
        print(f"     Offered odds: {_fmt_odds(opp['american_odds'])} "
              f"({opp['decimal_odds']:.4f})")
        fair_dec = 1.0 / opp["fair_prob"] if opp["fair_prob"] > 0 else 0
        fair_odds = 0
        if fair_dec >= 2.0:
            fair_odds = round((fair_dec - 1.0) * 100)
        elif fair_dec > 0:
            fair_odds = -round(100.0 / (fair_dec - 1.0))
        print(f"     Fair odds:    {_fmt_odds(fair_odds)}")
        print(f"     Fair prob:    {opp['fair_prob']:.4%}")
        print(f"     EV:           {_fmt_ev(opp['ev_pct'])}")
        if opp.get("pinnacle_approved") is not None:
            print(f"     Pinnacle:     {'APPROVED' if opp['pinnacle_approved'] else 'NOT APPROVED'}")
            print(f"       Ref prob:  {opp.get('pinnacle_fair_prob', 0):.4%}  "
                  f"EV {opp.get('pinnacle_ev', 0):+.2f}%  "
                  f"Prob edge {opp.get('pinnacle_prob_edge', 0):+.2f}%")
        else:
            print(f"     Pinnacle:     N/A (fallback reference)")
        print(f"     Books:        {opp['n_consensus_books']}")
        print(f"     Market qual:  {opp['market_quality']}")
        print(f"     Bet status:   {opp['bet_status']}")
        print(f"     Game:         {opp['away_team']} at {opp['home_team']}")
        print(f"     Start:        {opp['start_time'][:19].replace('T', ' ')}")
        print()

    for i, opp in enumerate(yn_opps, len(opps) + 1):
        rec_label = "RECOMMENDATION ELIGIBLE" if opp.get("rec_eligible") else "RESEARCH ONLY"
        print(f"  {i}. {opp['player_name']} YES  [YN \u2014 {rec_label}]")
        print(f"     Sportsbook:          {opp['sportsbook']}")
        print(f"     Offered odds:        {_fmt_odds(opp['american_odds'])} "
              f"({opp['decimal_odds']:.4f})")
        print(f"     Market ref prob:     {opp['market_reference_probability']:.4%}")
        print(f"     Market ref odds:     {_fmt_odds(opp['market_reference_odds'])}")
        print(f"     Price advantage:     {opp['price_advantage_pct']:+.2f}%")
        print(f"     Relative payout:     {opp['relative_payout_advantage_pct']:+.2f}%")
        print(f"     Decimal odds adv:   {opp['decimal_odds_advantage']:+d}")
        print(f"     Comparison status:   {opp['comparison_status']}")
        print(f"     Books:               {opp['n_consensus_books']}")
        print(f"     Game:                {opp['away_team']} at {opp['home_team']}")
        print(f"     Start:               {opp['start_time'][:19].replace('T', ' ')}")
        print()


# ==================================================================
# CLI
# ==================================================================

VALID_MARKETS = [m.cli_name for m in cfg.MARKET_REGISTRY]


def build_parser() -> argparse.ArgumentParser:
    """Build the generic scanner argument parser."""
    parser = argparse.ArgumentParser(
        description="MLB Player Prop Edge Scanner",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true",
                      help="Show all market sides (including NO_EDGE, research-only)")
    mode.add_argument("--positive-only", action="store_true",
                      help="Show only recommendation-eligible positive EV sides")
    mode.add_argument("--actionable-only", action="store_true",
                      help="Show only recommendation-eligible sides above actionable threshold (default)")
    parser.add_argument("--min-ev", type=float, default=None,
                        help=("Override actionable EV minimum (default "
                              f"{cfg.ACTIONABLE_EDGE_THRESHOLD:.0%}). O/U only.")
                        .replace("%", "%%"))
    parser.add_argument("--limit", type=int, default=25,
                        help="Max opportunities to display (default 25)")
    parser.add_argument("--market", choices=VALID_MARKETS + ["all"], default="all",
                        help=f"Market to scan (choices: {', '.join(VALID_MARKETS)}, all)")
    parser.add_argument("--market-form", choices=["ou", "yn", "all"], default="all",
                        help="Market form: ou (over/under), yn (yes/no), all (default)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed per-opportunity output")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging (Pinnacle diagnostics)")
    parser.add_argument("--sportsbook", type=str, default=None,
                        help="Filter by sportsbook name (case-insensitive substring)")
    parser.add_argument("--player", type=str, default=None,
                        help="Filter by player name (case-insensitive substring)")
    parser.add_argument("--game", type=str, default=None,
                        help="Filter by game/team name (case-insensitive substring)")
    parser.add_argument("--require-fresh", action="store_true",
                        help="Exit with error if data is stale (exceeds freshness threshold)")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

    # Validate config at startup
    config_errors = cfg.validate_config()
    if config_errors:
        for err in config_errors:
            print(f"CONFIG ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    if args.all:
        mode = "all"
    elif args.positive_only:
        mode = "positive"
    else:
        mode = "actionable"

    min_ev = args.min_ev
    if min_ev is not None and (min_ev < 0 or min_ev > 1):
        print("ERROR: --min-ev must be between 0 and 1 (e.g. 0.02 for 2%)",
              file=sys.stderr)
        sys.exit(1)

    # --min-ev is meaningless for YN markets (no complementary price → no EV).
    # Reject when the user explicitly requests YN-only form.
    if min_ev is not None and args.market_form == "yn":
        print("ERROR: --min-ev cannot be used with --market-form yn. "
              "EV is not computed for Yes/No markets (no complementary price). "
              "Use --min-ev with --market-form ou or omit --market-form.",
              file=sys.stderr)
        sys.exit(1)

    result = run_scan(
        mode=mode,
        min_ev=min_ev,
        limit=args.limit,
        market=args.market,
        market_form=args.market_form,
        sportsbook=args.sportsbook,
        player=args.player,
        game=args.game,
    )

    if args.verbose and (result["opportunities"] or result.get("yn_opportunities")):
        display_verbose(result)
    else:
        display_results(result, mode)

    # --require-fresh: exit nonzero if data is stale
    if args.require_fresh and result.get("stale_warning"):
        print("ERROR: Data is stale (exceeds freshness threshold). "
              "Use --require-fresh only when fresh data is required.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
