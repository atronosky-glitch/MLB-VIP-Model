#!/usr/bin/env python3
"""
MLB Model — Main Entry Point

Stage 3: Fetch today's MLB games, parse all odds into SQLite,
then run market analysis to find pricing inefficiencies.
"""

import logging
import sys

from dotenv import load_dotenv

from database.db_manager import (
    init_db,
    get_connection,
    save_game,
    save_raw_response,
    save_odds_batch,
    record_pull,
    create_run,
    finish_run,
    log_ingestion,
)
from src.api_client import SportsGameOddsClient
from src.odds_parser import parse_odds
from src.market_analysis import (
    american_to_probability,
    american_to_decimal,
    analyze_two_way_market,
    analyze_side,
    remove_vig,
    consensus_price,
)
from src.validation_constants import APPROVED_STATUSES, STATUS_POSSIBLE_MAPPING_ERROR, STATUS_INVALID_MAPPING, STATUS_NONE

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/main.log", mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _parse_status(status_obj: dict | str) -> str:
    if isinstance(status_obj, str):
        return status_obj
    if not isinstance(status_obj, dict):
        return "scheduled"
    if status_obj.get("cancelled"):
        return "cancelled"
    if status_obj.get("completed") or status_obj.get("finalized"):
        return "completed"
    if status_obj.get("live"):
        return "live"
    if status_obj.get("started"):
        return "started"
    return "scheduled"


def _pretty_odds(price: int) -> str:
    if price > 0:
        return f"+{price}"
    return str(price)


def _count_excluded(conn, event_id: str) -> dict:
    """Count records by exclusion category for the validation summary."""
    counts = {"approved": 0, "suspicious": 0, "invalid": 0, "unverified": 0, "other_excluded": 0}
    cur = conn.execute(
        """
        SELECT validation_status, COUNT(*) as cnt
        FROM odds
        WHERE event_id = ? AND is_alt_line = 0 AND available = 1
          AND sportsbook != '_audit'
        GROUP BY validation_status
        """,
        (event_id,),
    )
    for row in cur.fetchall():
        vs = row["validation_status"]
        c = row["cnt"]
        if vs in APPROVED_STATUSES:
            counts["approved"] += c
        elif vs == STATUS_POSSIBLE_MAPPING_ERROR:
            counts["suspicious"] += c
        elif vs == STATUS_INVALID_MAPPING:
            counts["invalid"] += c
        elif vs in (STATUS_NONE, "UNVERIFIED", "UNKNOWN"):
            counts["unverified"] += c
        else:
            counts["other_excluded"] += c
    return counts


def _build_validation_map(conn, event_id: str, market_pattern: str) -> dict[str, str]:
    """Build a {sportsbook: validation_status} dict for a given market."""
    cur = conn.execute(
        """
        SELECT sportsbook, validation_status
        FROM odds
        WHERE event_id = ? AND is_alt_line = 0 AND available = 1
          AND market LIKE ?
        """,
        (event_id, market_pattern),
    )
    vmap = {}
    for row in cur.fetchall():
        vmap[row["sportsbook"]] = row["validation_status"]
    return vmap


def _analyze_game_moneyline(conn, event_id: str, away_name: str, home_name: str):
    """Analyze moneyline market for a game and print results."""
    # Build per-side validation maps from the database
    vmap_away = _build_validation_map(conn, event_id, '%-game-ml-away')
    vmap_home = _build_validation_map(conn, event_id, '%-game-ml-home')

    cur = conn.execute(
        """
        SELECT sportsbook, market, price
        FROM odds
        WHERE event_id = ? AND is_alt_line = 0 AND available = 1
          AND (market LIKE '%-game-ml-%')
          AND validation_status IN ('VALID', 'CONFIRMED', 'VERIFIED')
        ORDER BY market, sportsbook
        """,
        (event_id,),
    )

    away_prices = {}
    home_prices = {}
    for row in cur.fetchall():
        mkt = row["market"]
        if "-ml-away" in mkt:
            away_prices[row["sportsbook"]] = row["price"]
        elif "-ml-home" in mkt:
            home_prices[row["sportsbook"]] = row["price"]

    if not away_prices or not home_prices:
        counts = _count_excluded(conn, event_id)
        print(f"\n  +-- MONEYLINE {'-' * 47}+")
        print(f"  | {away_name:<25} vs {home_name:<25} |")
        print(f"  | {'INSUFFICIENT APPROVED DATA':^55} |")
        _print_validation_summary(counts)
        print(f"  +{'--' * 40}+")
        return

    result = analyze_two_way_market(
        away_prices, home_prices,
        label_a=away_name, label_b=home_name,
        validation_map_a=vmap_away,
        validation_map_b=vmap_home,
    )

    sa = result["side_a"]
    sb = result["side_b"]

    counts = _count_excluded(conn, event_id)

    print(f"\n  +-- MONEYLINE {'-' * 47}+")
    print(f"  | {away_name:<25} vs {home_name:<25} |")
    print(f"  | Consensus:   {away_name:<12} {_pretty_odds(sa['consensus_price']):>6}"
          f"  {home_name:<12} {_pretty_odds(sb['consensus_price']):>6}      |")
    print(f"  | No-vig prob: {away_name:<12} {result['nv_prob_a']:.1%}"
          f"  {home_name:<12} {result['nv_prob_b']:.1%}      |")
    print(f"  | Market vig:  {result['vig_pct']:.2f}%                                |")
    print(f"  | Approved:    {sa['n_books']} books / {sa['n_books'] + sb['n_books']} sides                     |")

    if result["best_ev"]:
        best = result["best_ev"]
        print(f"  | Best EV:     {best['sportsbook']:<12} on {best['side']:<20}"
              f"  EV={best['ev']:+.2%}  |")

    _print_validation_summary(counts)
    print(f"  +{'--' * 40}+")

    # Detailed per-book table with validation status
    print(f"\n  {'Sportsbook':<15} {'Team':<22} {'Odds':>7} {'Status':<25} {'Incl.':<6} {'Reason'}")
    print(f"  {'-'*15} {'-'*22} {'-'*7} {'-'*25} {'-'*6} {'-'*40}")

    for book, price in sorted(away_prices.items(), key=lambda x: american_to_decimal(x[1]), reverse=True):
        vs = vmap_away.get(book, "UNKNOWN")
        incl = "YES" if vs in APPROVED_STATUSES else "NO"
        reason = _exclusion_reason(vs)
        print(f"  {book:<15} {away_name:<22} {_pretty_odds(price):>7} {vs:<25} {incl:<6} {reason}")

    for book, price in sorted(home_prices.items(), key=lambda x: american_to_decimal(x[1]), reverse=True):
        vs = vmap_home.get(book, "UNKNOWN")
        incl = "YES" if vs in APPROVED_STATUSES else "NO"
        reason = _exclusion_reason(vs)
        print(f"  {book:<15} {home_name:<22} {_pretty_odds(price):>7} {vs:<25} {incl:<6} {reason}")


def _exclusion_reason(status: str) -> str:
    reasons = {
        STATUS_POSSIBLE_MAPPING_ERROR: "Sign inverse of consensus — possible swap",
        STATUS_INVALID_MAPPING: "Participant ID mismatch",
        STATUS_NONE: "Unrecognized entity ID",
        "UNVERIFIED": "No verification possible",
        "UNKNOWN": "Unknown status",
    }
    return reasons.get(status, "")


def _print_validation_summary(counts: dict) -> None:
    sep = "-" * 53
    print(f"  | {sep} |")
    print(f"  | Validation summary:                                            |")
    print(f"  |   Approved:  {counts['approved']:>4} records{' ' * 35}|")
    if counts['suspicious']:
        print(f"  |   Suspicious (POSSIBLE_MAPPING_ERROR): {counts['suspicious']:>4}{' ' * 27}|")
    if counts['invalid']:
        print(f"  |   Invalid (INVALID_MAPPING):           {counts['invalid']:>4}{' ' * 27}|")
    if counts['unverified']:
        print(f"  |   Unverified:                           {counts['unverified']:>4}{' ' * 27}|")
    if counts['other_excluded']:
        print(f"  |   Other excluded:                       {counts['other_excluded']:>4}{' ' * 27}|")


def _print_prices_table(prices: dict, label: str, nv_prob: float):
    if not prices:
        return
    sorted_books = sorted(prices.items(), key=lambda x: american_to_probability(x[1]), reverse=True)
    print(f"\n  {label} (no-vig prob: {nv_prob:.1%}):")
    print(f"  {'Sportsbook':<15} {'Price':>7} {'Imp. Prob':>10} {'Dec. Odds':>10}")
    print(f"  {'─'*15} {'─'*7} {'─'*10} {'─'*10}")
    for book, price in sorted_books:
        dec = american_to_decimal(price)
        prob = american_to_probability(price)
        print(f"  {book:<15} {_pretty_odds(price):>7} {prob:>8.1%} {dec:>8.4f}")


def main():
    logger.info("=" * 60)
    logger.info("MLB Model — Stage 3: Full odds pull + market analysis")
    logger.info("=" * 60)

    init_db()

    conn = get_connection()
    try:
        run_id = create_run(conn, run_type="ingestion", mode="full")
    finally:
        conn.close()

    client = SportsGameOddsClient()

    logger.info("Fetching MLB events from SportsGameOdds API...")
    data = client.get_events(league="MLB", odds_available=True, include_alt_lines=True)

    conn = get_connection()
    try:
        save_raw_response(conn, "/events", {"league": "MLB"}, data)
    finally:
        conn.close()

    events = data.get("data", data.get("events", []))
    if not events:
        logger.warning("No MLB events found.")
        return

    total_odds_rows = 0
    total_audit_rows = 0

    for event in events:
        event_id = event.get("eventID") or event.get("id")
        teams = event.get("teams", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}

        home_name = home.get("names", {}).get("long") or home.get("name") or "Unknown"
        away_name = away.get("names", {}).get("long") or away.get("name") or "Unknown"
        status_obj = event.get("status", {}) or {}
        start_time = (
            status_obj.get("startsAt")
            if isinstance(status_obj, dict)
            else event.get("startDate")
        )

        game_record = {
            "event_id": event_id,
            "league": "MLB",
            "away_team": away_name,
            "home_team": home_name,
            "start_time": start_time,
            "status": _parse_status(status_obj),
            "sport_id": event.get("sportID"),
            "league_id": event.get("leagueID"),
        }
        conn = get_connection()
        try:
            save_game(conn, game_record)
        finally:
            conn.close()

        parsed = parse_odds(event)
        odds_rows = parsed.odds_rows
        audit_rows = parsed.audit_rows

        if odds_rows:
            conn = get_connection()
            try:
                cnt = save_odds_batch(conn, odds_rows, audit_rows)
                total_odds_rows += cnt
                total_audit_rows += len(audit_rows)
                log_ingestion(conn, run_id, event_id, cnt, len(audit_rows))
            except Exception as e:
                logger.error("Failed to save odds for %s: %s", event_id, e)
                conn2 = get_connection()
                try:
                    log_ingestion(conn2, run_id, event_id, error_message=str(e))
                finally:
                    conn2.close()
            finally:
                conn.close()

        conn = get_connection()
        try:
            record_pull(conn, event_id, "morning")
        finally:
            conn.close()

    print()
    print("=" * 80)
    print("MARKET ANALYSIS")
    print("=" * 80)

    conn = get_connection()
    try:
        games = conn.execute(
            "SELECT event_id, away_team, home_team, start_time FROM games ORDER BY start_time"
        ).fetchall()

        for game in games:
            event_id = game["event_id"]
            away_name = game["away_team"]
            home_name = game["home_team"]
            start = (game["start_time"] or "")[:19].replace("T", " ")

            sep = "-" * 80
            print(f"\n{sep}")
            print(f"  {away_name} @ {home_name}  -  {start}")
            print(f"{sep}")

            _analyze_game_moneyline(conn, event_id, away_name, home_name)

            cur = conn.execute(
                """
                SELECT sportsbook, market, price, points
                FROM odds
                WHERE event_id = ? AND is_alt_line = 0 AND available = 1
                  AND market = 'points-all-game-ou-over'
                  AND validation_status IN ('VALID', 'CONFIRMED', 'VERIFIED')
                ORDER BY sportsbook
                """,
                (event_id,),
            )
            over_prices = {}
            ou_points = None
            for row in cur.fetchall():
                over_prices[row["sportsbook"]] = row["price"]
                ou_points = row["points"]

            cur = conn.execute(
                """
                SELECT sportsbook, market, price
                FROM odds
                WHERE event_id = ? AND is_alt_line = 0 AND available = 1
                  AND market = 'points-all-game-ou-under'
                  AND validation_status IN ('VALID', 'CONFIRMED', 'VERIFIED')
                ORDER BY sportsbook
                """,
                (event_id,),
            )
            under_prices = {}
            for row in cur.fetchall():
                under_prices[row["sportsbook"]] = row["price"]

            if over_prices and under_prices:
                over_analysis = analyze_side(over_prices)
                under_analysis = analyze_side(under_prices)
                nv_over, nv_under = remove_vig(
                    over_analysis["consensus_price"],
                    under_analysis["consensus_price"],
                )

                print(f"\n  +-- TOTAL (Over/Under) {'-' * 38}+")
                print(f"  | Line: {ou_points or 'N/A':>6}                                  |")
                print(f"  | Consensus:   Over {_pretty_odds(over_analysis['consensus_price']):>6}"
                      f"  Under {_pretty_odds(under_analysis['consensus_price']):>6}      |")
                print(f"  | No-vig prob: Over {nv_over:.1%}  "
                      f"Under {nv_under:.1%}                     |")
                print(f"  +{'--' * 40}+")

            sp_away_points = None
            sp_home_points = None
            cur = conn.execute(
                """
                SELECT sportsbook, market, price, points
                FROM odds
                WHERE event_id = ? AND is_alt_line = 0 AND available = 1
                  AND market IN ('points-away-game-sp-away', 'points-home-game-sp-home')
                  AND validation_status IN ('VALID', 'CONFIRMED', 'VERIFIED')
                ORDER BY market, sportsbook
                """,
                (event_id,),
            )
            away_sp_prices = {}
            home_sp_prices = {}
            for row in cur.fetchall():
                mkt = row["market"]
                if "-sp-away" in mkt:
                    away_sp_prices[row["sportsbook"]] = row["price"]
                    sp_away_points = row["points"]
                elif "-sp-home" in mkt:
                    home_sp_prices[row["sportsbook"]] = row["price"]
                    sp_home_points = row["points"]

            if away_sp_prices and home_sp_prices:
                sp_away = analyze_side(away_sp_prices)
                sp_home = analyze_side(home_sp_prices)

                print(f"\n  +-- RUN LINE (Spread) {'-' * 38}+")
                print(f"  | Away spread: {sp_away_points or 'N/A':>6}     "
                      f"Home spread: {sp_home_points or 'N/A':>6}          |")
                print(f"  | Consensus:   {_pretty_odds(sp_away['consensus_price']):>6}"
                      f"              {_pretty_odds(sp_home['consensus_price']):>6}      |")
                print(f"  +{'--' * 40}+")

            # Player props summary (separate query with validation filter)
            cur = conn.execute(
                """
                SELECT sportsbook, market, price, points
                FROM odds
                WHERE event_id = ? AND is_alt_line = 0 AND available = 1
                  AND market LIKE 'batting_%'
                  AND (market LIKE '%-ou-over' OR market LIKE '%-yn-yes')
                  AND validation_status IN ('VALID', 'CONFIRMED', 'VERIFIED')
                ORDER BY market, sportsbook
                """,
                (event_id,),
            )

            from src.odds_parser import parse_odd_id_components
            from collections import defaultdict
            prop_groups = defaultdict(dict)
            for row in cur.fetchall():
                mkt = row["market"]
                comps = parse_odd_id_components(mkt)
                key = f"{comps['stat_id']}|{comps['entity_id']}"
                prop_groups[key][row["sportsbook"]] = row["price"]

            if prop_groups:
                prop_edges = []
                for key, prices in prop_groups.items():
                    if len(prices) < 2:
                        continue
                    cons = consensus_price(list(prices.values()))
                    cons_prob = american_to_probability(cons)
                    for book, price in prices.items():
                        prob = american_to_probability(price)
                        edge = (prob / cons_prob - 1) * 100
                        if edge > 0:
                            stat_name, player = key.split("|", 1)
                            prop_edges.append({
                                "stat": stat_name.replace("_", " ").title(),
                                "player": player,
                                "book": book,
                                "price": price,
                                "edge": edge,
                            })

                prop_edges.sort(key=lambda x: x["edge"], reverse=True)

                if prop_edges:
                    print(f"\n  +-- TOP PLAYER PROP EDGES {'-' * 32}+")
                    for p in prop_edges[:5]:
                        short_player = p["player"].replace("_", " ").replace(" MLB", "")
                        if len(short_player) > 28:
                            short_player = short_player[:25] + "..."
                        print(f"  | {short_player:<28} {p['stat']:<18} "
                              f"{p['book']:<12} {_pretty_odds(p['price']):>6} "
                              f"edge={p['edge']:+.1f}% |")
                    print(f"  +{'--' * 40}+")

    finally:
        conn.close()

    print()
    print("=" * 60)
    print(f"Stage 3 Complete")
    print(f"  Games:         {len(events)}")
    print(f"  Odds rows:     {total_odds_rows}")
    print(f"  Audit rows:    {total_audit_rows}")
    print("=" * 60)

    logger.info("Stage 3 complete. %s odds rows + %s audit rows stored.", total_odds_rows, total_audit_rows)

    conn = get_connection()
    try:
        finish_run(conn, run_id,
                   n_events=len(events),
                   n_opportunities=total_odds_rows,
                   data_source="LIVE API")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
