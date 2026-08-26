"""Analyse player prop over/under and yes/no markets.

For each exact O/U market group (same event, player, market type, line,
alt-line status), this module:

1. Collects approved Over and Under prices
2. When Pinnacle offers both sides, uses its no-vig probability as the
   sharp fair reference and compares every other book against it
3. Otherwise computes consensus independently per side using ALL available
   books (sportsbooks offering only one side contribute to same-side consensus)
4. Calculates LOO median implied probability as fair reference per side
5. Removes vig when both sides are available; uses raw implied otherwise
6. Computes EV for each book using same-side fair probability
7. Separates market quality from individual bet status

For YN market groups (same event, player, yes/no type):

1. Collects approved Yes-side prices
2. Computes LOO median implied probability as reference
3. Calculates price advantage metrics (not EV)
4. Classifies each book's price relative to the market

A market may be VALID_MARKET while every bet has NO_EDGE.
"""

from __future__ import annotations

import logging
from statistics import mean, median

from . import prop_config as cfg
from .validation_constants import APPROVED_STATUSES
from .market_analysis import american_to_decimal, american_to_probability

logger = logging.getLogger(__name__)


# ==================================================================
# Odds / probability helpers (Pinnacle value model)
# ==================================================================

def american_to_implied_prob(odds: int) -> float:
    """Convert American odds to implied probability (0..1)."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def american_to_decimal(odds: int) -> float:
    """Convert American odds to decimal odds (1 + payout fraction)."""
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def calculate_no_vig_probs(over_odds: int, under_odds: int) -> tuple[float, float]:
    """Remove vig from a two-outcome market.

    Returns ``(fair_over_prob, fair_under_prob)`` summing to 1.0.
    """
    raw_over = american_to_implied_prob(over_odds)
    raw_under = american_to_implied_prob(under_odds)
    total = raw_over + raw_under
    if total <= 0:
        return (0.5, 0.5)
    return (raw_over / total, raw_under / total)


def calculate_ev(true_probability: float, offered_odds: int) -> float:
    """Expected value: ``true_probability * decimal_odds - 1``."""
    return true_probability * american_to_decimal(offered_odds) - 1.0


def is_pinnacle_book(book_name: str) -> bool:
    """Case-insensitive Pinnacle detection, including common variants.

    Accepts ``pinnacle``, ``pinnacle sports``, ``pinny``, and any name
    containing ``pinnacle`` (e.g. ``Pinnacle Sportsbook``).
    """
    if not book_name:
        return False
    name = book_name.strip().lower()
    if name in ("pinnacle", "pinnacle sports", "pinny"):
        return True
    return "pinnacle" in name


def _group_key_meta(group_key: str) -> tuple[str, str]:
    """Best-effort ``(player_id, market_type)`` extraction for logging.

    Group keys look like ``{event_id}|{player_id}|{market_type}|game|{line}``.
    Returns empty strings when the key cannot be parsed.
    """
    parts = (group_key or "").split("|")
    player = parts[1] if len(parts) > 1 else ""
    market = parts[2] if len(parts) > 2 else ""
    return player, market


def _is_official(pinnacle_approved, ev: float) -> bool:
    """Whether a book entry may become an official pick.

    When ``REQUIRE_PINNACLE_FOR_OFFICIAL`` is enabled, only a Pinnacle-approved
    book (both-sides Pinnacle reference at the exact line with EV and
    probability-edge thresholds passed) may be official — fallback/market-median
    books are never official.  Otherwise the legacy rule applies: any book with
    positive EV is a candidate.
    """
    if getattr(cfg, "REQUIRE_PINNACLE_FOR_OFFICIAL", False):
        return bool(pinnacle_approved)
    return bool(ev > 0)


# ==================================================================
# Pinnacle diagnostics (logging only — never changes pick logic)
# ==================================================================

def _rejection_reason(*, market_quality, use_pinnacle_ref, pinnacle_found,
                      pinnacle_anywhere, pinnacle_both_sides, best_ev,
                      official_count) -> str:
    """Categorize why a group was rejected (or approved).

    Diagnostics only — no thresholds or pick logic are changed here.
    """
    if market_quality in (cfg.MARKET_QUALITY_EXCLUDED, cfg.MARKET_QUALITY_INSUFFICIENT):
        return "insufficient_comparison_books"
    if official_count > 0:
        return "approved"
    if use_pinnacle_ref:
        if best_ev is None:
            return "no_positive_edge"
        pev = best_ev.get("pinnacle_ev")
        if pev is None:
            return "pinnacle_threshold_failed"
        pedge = best_ev.get("pinnacle_prob_edge") or 0.0
        if pev < cfg.MIN_PINNACLE_EV * 100:
            return "ev_threshold_failed"
        if pedge < cfg.MIN_PINNACLE_PROB_EDGE * 100:
            return "prob_edge_threshold_failed"
        return "pinnacle_threshold_failed"
    if pinnacle_found and pinnacle_both_sides:
        return "pinnacle_model_disabled"
    if pinnacle_found:
        return "pinnacle_missing_opposite_side"
    if pinnacle_anywhere:
        return "pinnacle_line_mismatch"
    return "missing_pinnacle"


def _build_group_diagnostics(
    *,
    group_key: str,
    line,
    all_books: list[str],
    over_prices: dict,
    under_prices: dict,
    use_pinnacle_ref: bool,
    pinnacle_found: bool,
    pinnacle_over_book: str | None,
    pinnacle_under_book: str | None,
    pinnacle_over_price,
    pinnacle_under_price,
    nv_prob_over: float,
    nv_prob_under: float,
    best_ev: dict | None,
    official_count: int,
    market_quality: str,
) -> dict:
    """Structured diagnostics for one prop group (debug logging only)."""
    player_log, market_log = _group_key_meta(group_key)

    pinnacle_anywhere = sorted(b for b in all_books if is_pinnacle_book(b))
    pinnacle_both_sides = pinnacle_over_book is not None and pinnacle_under_book is not None

    rec_books = [b for b in all_books if not is_pinnacle_book(b)]
    over_recs = [b for b in rec_books if b in over_prices]
    under_recs = [b for b in rec_books if b in under_prices]
    best_over = (max(over_recs, key=lambda b: american_to_decimal(over_prices[b]["price"]))
                 if over_recs else None)
    best_under = (max(under_recs, key=lambda b: american_to_decimal(under_prices[b]["price"]))
                  if under_recs else None)

    if use_pinnacle_ref:
        fair_over, fair_under = nv_prob_over, nv_prob_under
    else:
        fair_over = fair_under = None

    best_sportsbook = best_ev.get("sportsbook") if best_ev else None
    best_side = best_ev.get("side") if best_ev else None
    best_odds = best_ev.get("american_odds") if best_ev else None
    best_ev_pct = best_ev.get("ev_pct") if best_ev else None
    best_pinnacle_ev = best_ev.get("pinnacle_ev") if best_ev else None
    best_prob_edge = best_ev.get("pinnacle_prob_edge") if best_ev else None

    reason = _rejection_reason(
        market_quality=market_quality,
        use_pinnacle_ref=use_pinnacle_ref,
        pinnacle_found=pinnacle_found,
        pinnacle_anywhere=bool(pinnacle_anywhere),
        pinnacle_both_sides=pinnacle_both_sides,
        best_ev=best_ev,
        official_count=official_count,
    )

    return {
        "player": player_log,
        "market": market_log,
        "line": line,
        "side": best_side,
        "total_books": len(all_books),
        "n_comparison_books": len(rec_books) if use_pinnacle_ref else len(all_books),
        "pinnacle_present": bool(pinnacle_found),
        "pinnacle_books": pinnacle_anywhere,
        "pinnacle_both_sides": pinnacle_both_sides,
        "pinnacle_reference_used": bool(use_pinnacle_ref),
        "fallback_used": not bool(use_pinnacle_ref),
        "pinnacle_over_price": pinnacle_over_price,
        "pinnacle_under_price": pinnacle_under_price,
        "pinnacle_fair_over": round(fair_over, 6) if fair_over is not None else None,
        "pinnacle_fair_under": round(fair_under, 6) if fair_under is not None else None,
        "best_non_pinnacle_over": ({"book": best_over, "odds": over_prices[best_over]["price"]}
                                   if best_over else {}),
        "best_non_pinnacle_under": ({"book": best_under, "odds": under_prices[best_under]["price"]}
                                    if best_under else {}),
        "best_sportsbook": best_sportsbook,
        "best_side": best_side,
        "best_odds": best_odds,
        "best_ev_pct": best_ev_pct,
        "best_pinnacle_ev": best_pinnacle_ev,
        "best_pinnacle_prob_edge": best_prob_edge,
        "official_approved": bool(official_count > 0),
        "rejection_reason": reason,
    }


def _fmt_diag(value) -> str:
    return "None" if value is None else str(value)


def _log_group_diagnostics(diag: dict) -> None:
    """Emit one DEBUG line summarising a group's Pinnacle diagnostics."""
    logger.debug(
        "PINNACLE_GROUP player=%s market=%s line=%s side=%s total_books=%d "
        "comparison_books=%d pinnacle_present=%s pinnacle_books=%s "
        "pinnacle_both_sides=%s pinnacle_over_odds=%s pinnacle_under_odds=%s "
        "pinnacle_fair_over=%s pinnacle_fair_under=%s best_book_over=%s "
        "best_over_odds=%s best_book_under=%s best_under_odds=%s "
        "best_ev_book=%s best_ev_side=%s best_ev_odds=%s ev_pct=%s "
        "pinnacle_ev=%s prob_edge=%s official_approved=%s rejection_reason=%s",
        diag.get("player", ""), diag.get("market", ""), diag.get("line"),
        diag.get("side"), diag.get("total_books", 0), diag.get("n_comparison_books", 0),
        bool(diag.get("pinnacle_present")), ",".join(diag.get("pinnacle_books") or []),
        bool(diag.get("pinnacle_both_sides")),
        _fmt_diag(diag.get("pinnacle_over_price")), _fmt_diag(diag.get("pinnacle_under_price")),
        _fmt_diag(diag.get("pinnacle_fair_over")), _fmt_diag(diag.get("pinnacle_fair_under")),
        _fmt_diag((diag.get("best_non_pinnacle_over") or {}).get("book")),
        _fmt_diag((diag.get("best_non_pinnacle_over") or {}).get("odds")),
        _fmt_diag((diag.get("best_non_pinnacle_under") or {}).get("book")),
        _fmt_diag((diag.get("best_non_pinnacle_under") or {}).get("odds")),
        _fmt_diag(diag.get("best_sportsbook")), _fmt_diag(diag.get("best_side")),
        _fmt_diag(diag.get("best_odds")), _fmt_diag(diag.get("best_ev_pct")),
        _fmt_diag(diag.get("best_pinnacle_ev")), _fmt_diag(diag.get("best_pinnacle_prob_edge")),
        bool(diag.get("official_approved")), diag.get("rejection_reason", ""),
    )


def _empty_diagnostics(group_key: str, over_prices: dict, under_prices: dict,
                       line=None) -> dict:
    """Diagnostics for groups that never reached full analysis (empty/excluded)."""
    player_log, market_log = _group_key_meta(group_key)
    all_books = sorted(set(over_prices) | set(under_prices))
    return {
        "player": player_log,
        "market": market_log,
        "line": line if line is not None else _resolve_line(over_prices, under_prices),
        "side": None,
        "total_books": len(all_books),
        "n_comparison_books": len([b for b in all_books if not is_pinnacle_book(b)]),
        "pinnacle_present": False,
        "pinnacle_books": sorted(b for b in all_books if is_pinnacle_book(b)),
        "pinnacle_both_sides": False,
        "pinnacle_reference_used": False,
        "fallback_used": False,
        "pinnacle_over_price": None,
        "pinnacle_under_price": None,
        "pinnacle_fair_over": None,
        "pinnacle_fair_under": None,
        "best_non_pinnacle_over": {},
        "best_non_pinnacle_under": {},
        "best_sportsbook": None,
        "best_side": None,
        "best_odds": None,
        "best_ev_pct": None,
        "best_pinnacle_ev": None,
        "best_pinnacle_prob_edge": None,
        "official_approved": False,
        "rejection_reason": "insufficient_comparison_books",
    }


# ==================================================================
# Public entry point
# ==================================================================

def analyze_prop_group(
    group_key: str,
    over_prices: dict[str, dict],
    under_prices: dict[str, dict],
    n_excluded_rows: int = 0,
    n_approved_rows: int = 0,
    **kwargs,
) -> dict:
    """Analyse one market group (Over/Under at same line).

    UPDATED: When Pinnacle offers both sides (and the Pinnacle value model
    is enabled), its no-vig probability is the fair reference for every
    other book; EV and probability edge are computed against it and each
    book is flagged ``pinnacle_approved`` when both thresholds pass.
    Otherwise falls back to same-side LOO median consensus using all
    available books (unless ``REQUIRE_PINNACLE_FOR_OFFICIAL``).

    Parameters
    ----------
    group_key : str
        The market group key (for identification in results).
    over_prices : dict[str, dict]
        ``{sportsbook: {price: int, decimal_odds: float, line: float}}``
        Only approved rows should be passed.
    under_prices : dict[str, dict]
        Same structure for the Under side.
    n_excluded_rows : int
        Number of excluded audit rows for this group.
    n_approved_rows : int
        Number of approved rows for this group.

    Returns
    -------
    dict with keys:
        group_key, line,
        market_quality: market_quality_status,
        quality_flags: list[str],
        n_paired_books, n_approved_rows, n_excluded_rows,
        consensus_over, consensus_under,
        nv_prob_over, nv_prob_under, vig_pct,
        pinnacle_found: whether a Pinnacle book is present at this exact line,
        pinnacle_reference_used: whether Pinnacle no-vig was used as reference,
        pinnacle_book, pinnacle_over_price, pinnacle_under_price,
        official_count: number of books eligible to be official picks,
        books: list of per-book analysis dicts (each with ``is_official``),
        best_ev: best EV entry or None,
        recommendation: "BET" or "NO_BET"
    """
    if not over_prices and not under_prices:
        return _empty_result(group_key, over_prices, under_prices,
                             n_excluded_rows, n_approved_rows)

    # All unique books across both sides
    all_books = sorted(set(over_prices) | set(under_prices))
    line = _resolve_line(over_prices, under_prices)
    total_n = len(all_books)

    over_books_list = sorted(over_prices.keys())
    under_books_list = sorted(under_prices.keys())

    # Same-line guard: every price in the group must be at the resolved line.
    # Groups are built exact-line upstream; this defensively logs (rather than
    # silently merges) any line fragmentation.
    for side_label, side_prices in (("OVER", over_prices), ("UNDER", under_prices)):
        for book, entry in side_prices.items():
            entry_line = entry.get("line")
            if entry_line is None or line is None:
                continue
            try:
                if abs(float(entry_line) - float(line)) > 1e-9:
                    logger.warning(
                        "PINNACLE_LINE_FRAGMENTATION group=%s side=%s book=%s "
                        "entry_line=%s resolved_line=%s",
                        group_key, side_label, book, entry_line, line,
                    )
            except (TypeError, ValueError):
                logger.warning(
                    "PINNACLE_LINE_FRAGMENTATION group=%s side=%s book=%s entry_line=%r",
                    group_key, side_label, book, entry_line,
                )

    # Market quality — check early to avoid crash on empty book list
    market_quality, quality_flags = _classify_market(total_n, all_books, over_prices, under_prices)
    if market_quality == cfg.MARKET_QUALITY_EXCLUDED:
        return _empty_result(group_key, over_prices, under_prices,
                             n_excluded_rows, n_approved_rows, line=line)

    # Pinnacle detection at the EXACT resolved line — a Pinnacle price on a
    # different line must never be used as the reference for this group.
    def _at_resolved_line(book: str) -> bool:
        entry = over_prices.get(book) or under_prices.get(book)
        if not entry or entry.get("line") is None or line is None:
            return True  # nothing to contradict
        try:
            return abs(float(entry["line"]) - float(line)) <= 1e-9
        except (TypeError, ValueError):
            return True

    pinnacle_over_book = next((b for b in over_books_list
                               if is_pinnacle_book(b) and _at_resolved_line(b)), None)
    pinnacle_under_book = next((b for b in under_books_list
                                if is_pinnacle_book(b) and _at_resolved_line(b)), None)
    pinnacle_found = any(is_pinnacle_book(b) and _at_resolved_line(b) for b in all_books)
    use_pinnacle_model = bool(getattr(cfg, "USE_PINNACLE_VALUE_MODEL", True))
    use_pinnacle_ref = (use_pinnacle_model
                        and pinnacle_over_book is not None
                        and pinnacle_under_book is not None)

    player_log, market_log = _group_key_meta(group_key)
    pinnacle_over_price = None
    pinnacle_under_price = None

    pinnacle_source = None
    if use_pinnacle_ref:
        # Pinnacle no-vig probability becomes the fair reference for all books
        pinnacle_over_price = over_prices[pinnacle_over_book]["price"]
        pinnacle_under_price = under_prices[pinnacle_under_book]["price"]
        # Provenance (odds_api_pinnacle / direct_pinnapi) — both injectors
        # write the same key; an organically-fetched (never injected)
        # pinnacle book has no such key, so this stays None for that case.
        pinnacle_source = (
            over_prices[pinnacle_over_book].get("pinnacle_source")
            or under_prices[pinnacle_under_book].get("pinnacle_source")
        )
        nv_prob_over, nv_prob_under = calculate_no_vig_probs(
            pinnacle_over_price, pinnacle_under_price)
        total_prob = (american_to_implied_prob(pinnacle_over_price)
                      + american_to_implied_prob(pinnacle_under_price))
        consensus_over = pinnacle_over_price
        consensus_under = pinnacle_under_price
        vig_pct = (total_prob - 1.0) * 100.0
        print(f"    [PINNACLE-REF] {group_key}: Pinnacle ({pinnacle_over_book}) "
              f"{pinnacle_over_price}/{pinnacle_under_price} "
              f"no-vig {nv_prob_over:.3f}/{nv_prob_under:.3f}  vig {vig_pct:.1f}%")

        # Best recreational book per side (Pinnacle excluded from best-price search)
        rec_books = [b for b in all_books if not is_pinnacle_book(b)]
        over_recs = [b for b in rec_books if b in over_prices]
        under_recs = [b for b in rec_books if b in under_prices]
        best_over = max(over_recs, key=lambda b: american_to_decimal(over_prices[b]["price"])) if over_recs else None
        best_under = max(under_recs, key=lambda b: american_to_decimal(under_prices[b]["price"])) if under_recs else None
        best_over_odds = over_prices[best_over]["price"] if best_over else None
        best_under_odds = under_prices[best_under]["price"] if best_under else None
        print(f"    [PINNACLE-REF] {group_key}: best recreational "
              f"OVER {best_over}:{best_over_odds}  UNDER {best_under}:{best_under_odds}")

        # Per-book analysis using Pinnacle as the fair reference
        books = []
        for book in all_books:
            if is_pinnacle_book(book):
                continue  # Skip Pinnacle itself — it's the reference, not a target
            has_over = book in over_prices
            has_under = book in under_prices

            if has_over:
                over_info = over_prices[book]
                over_price = over_info["price"]
                over_dec = american_to_decimal(over_price)
                ev_over = calculate_ev(nv_prob_over, over_price)
                prob_edge_over = nv_prob_over - american_to_implied_prob(over_price)
                approved_over = (ev_over >= cfg.MIN_PINNACLE_EV
                                 and prob_edge_over >= cfg.MIN_PINNACLE_PROB_EDGE)
                bet_status_over = _classify_bet(ev_over)
                books.append({
                    "sportsbook": book,
                    "side": "OVER",
                    "line": line,
                    "american_odds": over_price,
                    "decimal_odds": round(over_dec, 4),
                    "fair_prob": round(nv_prob_over, 6),
                    "ev_pct": round(ev_over * 100, 4),
                    "bet_status": bet_status_over,
                    "validation_status": over_info.get("validation_status", "VALID"),
                    "included": True,
                    "reason": "",
                    "pinnacle_ev": round(ev_over * 100, 4),
                    "pinnacle_prob_edge": round(prob_edge_over * 100, 4),
                    "pinnacle_fair_prob": round(nv_prob_over, 6),
                    "pinnacle_approved": bool(approved_over),
                    "is_official": _is_official(bool(approved_over), ev_over),
                })

            if has_under:
                under_info = under_prices[book]
                under_price = under_info["price"]
                under_dec = american_to_decimal(under_price)
                ev_under = calculate_ev(nv_prob_under, under_price)
                prob_edge_under = nv_prob_under - american_to_implied_prob(under_price)
                approved_under = (ev_under >= cfg.MIN_PINNACLE_EV
                                  and prob_edge_under >= cfg.MIN_PINNACLE_PROB_EDGE)
                bet_status_under = _classify_bet(ev_under)
                books.append({
                    "sportsbook": book,
                    "side": "UNDER",
                    "line": line,
                    "american_odds": under_price,
                    "decimal_odds": round(under_dec, 4),
                    "fair_prob": round(nv_prob_under, 6),
                    "ev_pct": round(ev_under * 100, 4),
                    "bet_status": bet_status_under,
                    "validation_status": under_info.get("validation_status", "VALID"),
                    "included": True,
                    "reason": "",
                    "pinnacle_ev": round(ev_under * 100, 4),
                    "pinnacle_prob_edge": round(prob_edge_under * 100, 4),
                    "pinnacle_fair_prob": round(nv_prob_under, 6),
                    "pinnacle_approved": bool(approved_under),
                    "is_official": _is_official(bool(approved_under), ev_under),
                })

        for b in books:
            if b.get("pinnacle_approved"):
                print(f"    [PINNACLE-REF] {group_key}: PINNACLE-APPROVED {b['sportsbook']} "
                      f"{b['side']} @ {b['american_odds']}  EV {b['pinnacle_ev']:+.2f}%  "
                      f"prob_edge {b['pinnacle_prob_edge']:+.2f}%")
    else:
        # Fallback: consensus from ALL available prices per side (not just paired)
        over_odds_list = [over_prices[b]["price"] for b in over_books_list]
        under_odds_list = [under_prices[b]["price"] for b in under_books_list]
        consensus_over = _consensus(over_odds_list) if over_odds_list else 0
        consensus_under = _consensus(under_odds_list) if under_odds_list else 0

        # Vig removal when both sides have data, otherwise use raw implied probability
        if over_odds_list and under_odds_list:
            nv_prob_over, nv_prob_under = _remove_vig(consensus_over, consensus_under)
            vig_pct = _vig_percentage(consensus_over, consensus_under)
        else:
            if over_odds_list:
                over_implied = [american_to_probability(p) for p in over_odds_list]
                nv_prob_over = median(over_implied)
            else:
                nv_prob_over = 0.0
            if under_odds_list:
                under_implied = [american_to_probability(p) for p in under_odds_list]
                nv_prob_under = median(under_implied)
            else:
                nv_prob_under = 0.0
            vig_pct = 0.0

        # Books that have BOTH Over and Under (for paired LOO)
        paired_books = sorted(set(over_prices) & set(under_prices))

        # Per-book analysis with LOO fair probability and bet_status
        books = []
        for book in all_books:
            has_over = book in over_prices
            has_under = book in under_prices
            is_paired = has_over and has_under

            if is_paired and len(paired_books) >= 3:
                # Paired LOO with vig removal (most accurate when both sides exist)
                other_paired = [b for b in paired_books if b != book]
                other_over_prices = [over_prices[b]["price"] for b in other_paired]
                other_under_prices = [under_prices[b]["price"] for b in other_paired]
                loo_cons_over = _consensus(other_over_prices)
                loo_cons_under = _consensus(other_under_prices)
                loo_fair_over, loo_fair_under = _remove_vig(loo_cons_over, loo_cons_under)
            else:
                loo_fair_over = nv_prob_over
                loo_fair_under = nv_prob_under

            if has_over:
                over_info = over_prices[book]
                over_price = over_info["price"]

                if not is_paired or len(paired_books) < 3:
                    # Single-side LOO: median of other Over implied probabilities
                    other_over_probs = [american_to_probability(over_prices[b]["price"])
                                       for b in over_books_list if b != book]
                    loo_fair_over = median(other_over_probs) if len(other_over_probs) >= 2 else nv_prob_over

                over_dec = american_to_decimal(over_price)
                ev_over = loo_fair_over * over_dec - 1.0
                bet_status_over = _classify_bet(ev_over)

                books.append({
                    "sportsbook": book,
                    "side": "OVER",
                    "line": line,
                    "american_odds": over_price,
                    "decimal_odds": round(over_dec, 4),
                    "fair_prob": round(loo_fair_over, 6),
                    "ev_pct": round(ev_over * 100, 4),
                    "bet_status": bet_status_over,
                    "validation_status": over_info.get("validation_status", "VALID"),
                    "included": True,
                    "reason": "",
                    "pinnacle_ev": None,
                    "pinnacle_prob_edge": None,
                    "pinnacle_fair_prob": None,
                    "pinnacle_approved": None,
                    "is_official": _is_official(None, ev_over),
                })

            if has_under:
                under_info = under_prices[book]
                under_price = under_info["price"]

                if not is_paired or len(paired_books) < 3:
                    # Single-side LOO: median of other Under implied probabilities
                    other_under_probs = [american_to_probability(under_prices[b]["price"])
                                        for b in under_books_list if b != book]
                    loo_fair_under = median(other_under_probs) if len(other_under_probs) >= 2 else nv_prob_under

                under_dec = american_to_decimal(under_price)
                ev_under = loo_fair_under * under_dec - 1.0
                bet_status_under = _classify_bet(ev_under)

                books.append({
                    "sportsbook": book,
                    "side": "UNDER",
                    "line": line,
                    "american_odds": under_price,
                    "decimal_odds": round(under_dec, 4),
                    "fair_prob": round(loo_fair_under, 6),
                    "ev_pct": round(ev_under * 100, 4),
                    "bet_status": bet_status_under,
                    "validation_status": under_info.get("validation_status", "VALID"),
                    "included": True,
                    "reason": "",
                    "pinnacle_ev": None,
                    "pinnacle_prob_edge": None,
                    "pinnacle_fair_prob": None,
                    "pinnacle_approved": None,
                    "is_official": _is_official(None, ev_under),
                })

    # If any extreme outlier, demote market quality
    if any(b["ev_pct"] > cfg.OUTLIER_EV_THRESHOLD * 100 or b["ev_pct"] < -cfg.OUTLIER_EV_THRESHOLD * 100
           for b in books if b["included"]):
        if market_quality == cfg.MARKET_QUALITY_VALID:
            market_quality = cfg.MARKET_QUALITY_NEEDS_REVIEW
            quality_flags.append("Extreme outlier EV detected")

    books.sort(key=lambda b: b["ev_pct"], reverse=True)

    # Best positive EV
    best_ev = None
    for b in books:
        if b["ev_pct"] > 0 and b["included"]:
            best_ev = {
                "sportsbook": b["sportsbook"],
                "side": b["side"],
                "ev_pct": b["ev_pct"],
                "american_odds": b["american_odds"],
                "bet_status": b["bet_status"],
                "pinnacle_approved": b.get("pinnacle_approved"),
                "pinnacle_ev": b.get("pinnacle_ev"),
                "pinnacle_prob_edge": b.get("pinnacle_prob_edge"),
                "is_official": b.get("is_official", False),
            }
            break

    recommendation = "BET" if best_ev is not None else "NO_BET"

    # Pinnacle value model logging + official-eligibility gating.
    # Fallback opportunities are STILL displayed (best_ev kept) but are never
    # official when REQUIRE_PINNACLE_FOR_OFFICIAL is enabled.
    if not use_pinnacle_ref:
        if not use_pinnacle_model:
            print(f"    [PINNACLE-REF] {group_key}: Pinnacle value model disabled — LOO median used")
        elif getattr(cfg, "PINNACLE_FALLBACK_TO_MARKET_MEDIAN", True):
            print(f"    [PINNACLE-REF] {group_key}: Pinnacle missing or one-sided — LOO median fallback used")
        else:
            print(f"    [PINNACLE-REF] {group_key}: Pinnacle missing and market-median fallback disabled")
    else:
        print(f"    [PINNACLE-REF] {group_key}: Pinnacle reference active — no-vig EV and prob edge vs Pinnacle")

    # Debug log: per-group Pinnacle check summary (live debugging / tests)
    if best_ev is not None:
        _chk_ev = best_ev.get("pinnacle_ev")
        _chk_edge = best_ev.get("pinnacle_prob_edge")
        if _chk_ev is None:
            _chk_ev = best_ev.get("ev_pct", 0.0)
            _chk_edge = 0.0
        _chk_approved = bool(best_ev.get("is_official", False))
    else:
        _chk_ev, _chk_edge, _chk_approved = 0.0, 0.0, False
    logger.debug(
        "PINNACLE_CHECK player=%s market=%s line=%s found=%s approved=%s ev=%.4f prob_edge=%.4f",
        player_log, market_log, line, bool(pinnacle_found), bool(_chk_approved),
        float(_chk_ev), float(_chk_edge),
    )

    official_books = [b for b in books if b.get("is_official") and b["included"]]

    # Fallback picks stay visible but are never official under the REQUIRE flag
    require_official = getattr(cfg, "REQUIRE_PINNACLE_FOR_OFFICIAL", False)
    if (require_official and best_ev is not None
            and not best_ev.get("is_official", False)):
        reason = "missing_pinnacle" if not use_pinnacle_ref else "pinnacle_threshold_failed"
        logger.warning(
            "OFFICIAL_BLOCKED_REQUIRE_PINNACLE player=%s market=%s line=%s reason=%s",
            player_log, market_log, line, reason,
        )

    # Pinnacle diagnostics (structured metadata + one DEBUG log per group)
    diagnostics = _build_group_diagnostics(
        group_key=group_key,
        line=line,
        all_books=all_books,
        over_prices=over_prices,
        under_prices=under_prices,
        use_pinnacle_ref=use_pinnacle_ref,
        pinnacle_found=pinnacle_found,
        pinnacle_over_book=pinnacle_over_book,
        pinnacle_under_book=pinnacle_under_book,
        pinnacle_over_price=pinnacle_over_price,
        pinnacle_under_price=pinnacle_under_price,
        nv_prob_over=nv_prob_over,
        nv_prob_under=nv_prob_under,
        best_ev=best_ev,
        official_count=len(official_books),
        market_quality=market_quality,
    )
    _log_group_diagnostics(diagnostics)

    return {
        "group_key": group_key,
        "line": line,
        "market_quality": market_quality,
        "quality_flags": quality_flags,
        "n_paired_books": total_n,
        "n_approved_rows": n_approved_rows or len(over_prices) + len(under_prices),
        "n_excluded_rows": n_excluded_rows,
        "consensus_over": consensus_over,
        "consensus_under": consensus_under,
        "nv_prob_over": round(nv_prob_over, 6),
        "nv_prob_under": round(nv_prob_under, 6),
        "vig_pct": round(vig_pct, 4),
        "pinnacle_found": bool(pinnacle_found),
        "pinnacle_reference_used": bool(use_pinnacle_ref),
        "pinnacle_book": pinnacle_over_book,
        "pinnacle_over_price": pinnacle_over_price,
        "pinnacle_under_price": pinnacle_under_price,
        "pinnacle_source": pinnacle_source,
        "official_count": len(official_books),
        "books": books,
        "best_ev": best_ev,
        "recommendation": recommendation,
        "diagnostics": diagnostics,
    }


# ==================================================================
# Helpers
# ==================================================================

def _empty_result(group_key, over_prices, under_prices,
                  n_excluded_rows=0, n_approved_rows=0, line=None):
    return {
        "group_key": group_key,
        "line": line if line is not None else _resolve_line(over_prices, under_prices),
        "market_quality": cfg.MARKET_QUALITY_EXCLUDED,
        "quality_flags": ["No paired Over/Under data"],
        "n_paired_books": 0,
        "n_approved_rows": n_approved_rows or len(over_prices) + len(under_prices),
        "n_excluded_rows": n_excluded_rows,
        "consensus_over": 0,
        "consensus_under": 0,
        "nv_prob_over": 0.0,
        "nv_prob_under": 0.0,
        "vig_pct": 0.0,
        "pinnacle_found": False,
        "pinnacle_reference_used": False,
        "pinnacle_book": None,
        "pinnacle_over_price": None,
        "pinnacle_under_price": None,
        "pinnacle_source": None,
        "official_count": 0,
        "books": [],
        "best_ev": None,
        "recommendation": "NO_BET",
        "diagnostics": _empty_diagnostics(group_key, over_prices, under_prices, line),
    }


def _classify_market(total_books, all_books, over_prices, under_prices):
    """Determine market quality based on total unique books across both sides."""
    flags = []
    if total_books < 2:
        return cfg.MARKET_QUALITY_EXCLUDED, flags + ["Fewer than 2 books"]
    if total_books < cfg.MIN_COMPARISON_BOOKS + 1:
        flags.append(f"Only {total_books} books (need {cfg.MIN_COMPARISON_BOOKS + 1})")
        return cfg.MARKET_QUALITY_INSUFFICIENT, flags
    return cfg.MARKET_QUALITY_VALID, flags


def _classify_bet(ev: float) -> str:
    """Classify a single bet based on EV percentage."""
    if ev >= cfg.STRONG_EDGE_THRESHOLD:
        return cfg.BET_STATUS_STRONG
    if ev >= cfg.POSITIVE_EDGE_THRESHOLD:
        return cfg.BET_STATUS_POSITIVE
    if ev > 0:
        return cfg.BET_STATUS_MARGINAL
    return cfg.BET_STATUS_NO_EDGE


def _consensus(prices: list[int]) -> int:
    """Implied-probability-space consensus."""
    if not prices:
        return 0
    probs = [american_to_probability(p) for p in prices]
    avg_prob = mean(probs)
    if avg_prob <= 0:
        return 0
    dec = 1.0 / avg_prob
    if dec >= 2.0:
        return round((dec - 1.0) * 100.0)
    return -round(100.0 / (dec - 1.0))


def _remove_vig(odds_a: int, odds_b: int) -> tuple[float, float]:
    """Remove vig from a two-outcome market."""
    imp_a = american_to_probability(odds_a)
    imp_b = american_to_probability(odds_b)
    total = imp_a + imp_b
    if total <= 0:
        return (0.5, 0.5)
    return (imp_a / total, imp_b / total)


def _vig_percentage(odds_a: int, odds_b: int) -> float:
    """Return vig as a percentage."""
    imp_a = american_to_probability(odds_a)
    imp_b = american_to_probability(odds_b)
    return (imp_a + imp_b - 1.0) * 100.0


def _resolve_line(over_prices, under_prices):
    """Get the line from the first available entry."""
    for d in (over_prices, under_prices):
        for entry in d.values():
            return entry.get("line")
    return None


# ==================================================================
# Yes/No (single-sided) analysis
# ==================================================================

def analyze_yn_group(
    group_key: str,
    yes_prices: dict[str, dict],
    n_excluded_rows: int = 0,
    n_approved_rows: int = 0,
    **kwargs,
) -> dict:
    """Analyse one Yes/No market group (single-sided price comparison).

    Reference method: median implied probability from LOO book set.
    No true EV is computed — only price advantage metrics.

    Parameters
    ----------
    group_key : str
        The market group key.
    yes_prices : dict[str, dict]
        ``{sportsbook: {price: int, decimal_odds: float}}``
        Only approved Yes-side rows should be passed.
    n_excluded_rows, n_approved_rows : int
        Row counts for reporting.

    Returns
    -------
    dict with keys:
        group_key, market_quality, quality_flags,
        n_books, n_approved_rows, n_excluded_rows,
        reference_book_count, reference_method,
        books: list of per-book comparison dicts,
        recommendation_eligible: bool
    """
    if not yes_prices:
        return _empty_yn_result(group_key, n_excluded_rows, n_approved_rows)

    all_books = sorted(yes_prices.keys())
    n_books = len(all_books)

    # Market quality check
    if n_books < cfg.YN_MIN_COMPARISON_BOOKS + 1:
        return {
            "group_key": group_key,
            "market_quality": cfg.MARKET_QUALITY_INSUFFICIENT,
            "quality_flags": [f"Only {n_books} books (need {cfg.YN_MIN_COMPARISON_BOOKS + 1})"],
            "n_books": n_books,
            "n_approved_rows": n_approved_rows or n_books,
            "n_excluded_rows": n_excluded_rows,
            "reference_book_count": 0,
            "reference_method": "LOO median implied probability",
            "books": [],
            "recommendation_eligible": False,
        }

    # Per-book analysis with LOO median reference
    books = []
    for book in all_books:
        offered_price = yes_prices[book]["price"]
        offered_dec = yes_prices[book]["decimal_odds"]
        offered_prob = american_to_probability(offered_price)

        # LOO: median implied probability from all OTHER books
        other_books = [b for b in all_books if b != book]
        other_probs = [american_to_probability(yes_prices[b]["price"]) for b in other_books]
        ref_prob = median(other_probs)
        ref_dec = 1.0 / ref_prob
        ref_american = _decimal_to_american_display(ref_dec)

        # Price advantage metrics
        price_advantage_pct = ref_prob - offered_prob  # positive = offered is better
        relative_payout_pct = (offered_dec / ref_dec - 1.0) * 100.0
        decimal_odds_adv = _compute_decimal_odds_advantage(offered_price, ref_american)

        comparison_status = _classify_yn_price(price_advantage_pct)

        books.append({
            "sportsbook": book,
            "side": "YES",
            "american_odds": offered_price,
            "decimal_odds": round(offered_dec, 4),
            "offered_implied_probability": round(offered_prob, 6),
            "market_reference_probability": round(ref_prob, 6),
            "market_reference_odds": ref_american,
            "price_advantage_pct": round(price_advantage_pct * 100, 4),
            "relative_payout_advantage_pct": round(relative_payout_pct, 4),
            "decimal_odds_advantage": decimal_odds_adv,
            "comparison_status": comparison_status,
            "recommendation_eligible": comparison_status in (
                cfg.YN_STATUS_STRONG_OUTLIER, cfg.YN_STATUS_OUTLIER,
            ),
            "validation_status": yes_prices[book].get("validation_status", "VALID"),
            "n_loo_books": len(other_books),
        })

    books.sort(key=lambda b: b["price_advantage_pct"], reverse=True)

    has_eligible = any(b["recommendation_eligible"] for b in books)

    return {
        "group_key": group_key,
        "market_quality": cfg.MARKET_QUALITY_VALID,
        "quality_flags": [],
        "n_books": n_books,
        "n_approved_rows": n_approved_rows or n_books,
        "n_excluded_rows": n_excluded_rows,
        "reference_book_count": n_books - 1,
        "reference_method": "LOO median implied probability",
        "books": books,
        "recommendation_eligible": has_eligible,
    }


def _empty_yn_result(
    group_key: str,
    n_excluded_rows: int = 0,
    n_approved_rows: int = 0,
    quality: str | None = None,
    flags: list[str] | None = None,
) -> dict:
    return {
        "group_key": group_key,
        "market_quality": quality or cfg.MARKET_QUALITY_EXCLUDED,
        "quality_flags": flags or ["No Yes-side data"],
        "n_books": 0,
        "n_approved_rows": n_approved_rows,
        "n_excluded_rows": n_excluded_rows,
        "reference_book_count": 0,
        "reference_method": "LOO median implied probability",
        "books": [],
        "recommendation_eligible": False,
    }


def _classify_yn_price(price_advantage_pct: float) -> str:
    """Classify a YN book's price relative to the LOO market reference.

    Parameters
    ----------
    price_advantage_pct : float
        Decimal difference: reference_prob - offered_prob.
        Positive means the offered price is better (lower implied probability).
    """
    if price_advantage_pct >= cfg.YN_STRONG_OUTLIER_THRESHOLD:
        return cfg.YN_STATUS_STRONG_OUTLIER
    if price_advantage_pct >= cfg.YN_OUTLIER_THRESHOLD:
        return cfg.YN_STATUS_OUTLIER
    if price_advantage_pct >= cfg.YN_MARGINAL_OUTLIER_THRESHOLD:
        return cfg.YN_STATUS_MARGINAL_OUTLIER
    if price_advantage_pct >= 0:
        return cfg.YN_STATUS_IN_LINE
    return cfg.YN_STATUS_WORSE


def _decimal_to_american_display(dec: float) -> int:
    """Convert decimal odds to American for display (no rounding of vig)."""
    if dec >= 2.0:
        return round((dec - 1.0) * 100.0)
    return -round(100.0 / (dec - 1.0))


def _compute_decimal_odds_advantage(offered: int, reference: int) -> int:
    """Compute decimal-odds advantage (decimal difference × 100).

    Positive means offered odds are better (higher payout per dollar).
    This is NOT American-odds cents; it is a decimal-scale metric.
    """
    offered_dec = american_to_decimal(offered)
    ref_dec = american_to_decimal(reference)
    return round((offered_dec - ref_dec) * 100)
