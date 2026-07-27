"""Market analysis engine.

Computes consensus odds, no-vig probabilities, EV-based edge detection,
outlier analysis, and CLV.

ALL price comparisons and edge calculations use decimal odds as the
canonical form to avoid the pitfalls of American odds (where higher
numbers can be *better* for underdogs but *worse* for favourites).

Defense in depth
-----------------
Analysis functions accept an optional ``validation_map`` parameter.
If provided, records with unapproved validation statuses are silently
excluded before any calculation.  The primary gate is SQL filtering,
but this provides a second layer of protection.
"""

import logging
from statistics import mean, stdev

from .validation_constants import APPROVED_STATUSES

logger = logging.getLogger(__name__)


# ======================================================================
# Conversion utilities
# ======================================================================

def american_to_decimal(odds: int) -> float:
    """Convert American odds to decimal odds."""
    if odds > 0:
        return 1.0 + odds / 100.0
    return 1.0 + 100.0 / abs(odds)


def decimal_to_american(decimal: float) -> int:
    """Convert decimal odds back to American odds (rounded)."""
    if decimal <= 1.0:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal}")
    if decimal >= 2.0:
        return round((decimal - 1.0) * 100.0)
    return -round(100.0 / (decimal - 1.0))


def american_to_probability(odds: int) -> float:
    """Convert American odds to implied probability (0..1)."""
    dec = american_to_decimal(odds)
    return 1.0 / dec


def probability_to_american(prob: float) -> int:
    """Convert implied probability (0..1) back to American odds (rounded)."""
    if prob <= 0.0 or prob >= 1.0:
        raise ValueError(f"Probability must be between 0 and 1, got {prob}")
    dec = 1.0 / prob
    return decimal_to_american(dec)


# ======================================================================
# Price comparison (always in decimal space)
# ======================================================================

def better_price(a: int, b: int) -> bool:
    """Return True if price *a* is better for the bettor than price *b*."""
    return american_to_decimal(a) > american_to_decimal(b)


def best_price(prices: list[int]) -> int:
    """Return the best (highest payout) American odds from a list."""
    return max(prices, key=american_to_decimal)


def worst_price(prices: list[int]) -> int:
    """Return the worst (lowest payout) American odds from a list."""
    return min(prices, key=american_to_decimal)


# ======================================================================
# No-vig / fair probability
# ======================================================================

def remove_vig(odds_a: int, odds_b: int) -> tuple[float, float]:
    """Compute no-vig (fair) probabilities for a two-outcome market."""
    imp_a = american_to_probability(odds_a)
    imp_b = american_to_probability(odds_b)
    total = imp_a + imp_b
    if total <= 0:
        return (0.5, 0.5)
    return (imp_a / total, imp_b / total)


def vig_percentage(odds_a: int, odds_b: int) -> float:
    """Return the vig (overround) as a percentage."""
    imp_a = american_to_probability(odds_a)
    imp_b = american_to_probability(odds_b)
    return (imp_a + imp_b - 1.0) * 100.0


# ======================================================================
# Consensus
# ======================================================================

def consensus_price(prices: list[int]) -> int:
    """Compute the consensus (average) American price from a list.

    Averages in decimal-odds space then converts back.
    """
    if not prices:
        return 0
    decs = [american_to_decimal(p) for p in prices]
    avg_dec = mean(decs)
    return decimal_to_american(avg_dec)


# ======================================================================
# Expected value
# ======================================================================

def expected_value(fair_probability: float, american_odds: int) -> float:
    """Compute expected value of a bet.

    EV = fair_probability * decimal_odds - 1

    Positive EV means the bet has value.
    """
    dec = american_to_decimal(american_odds)
    return fair_probability * dec - 1.0


# ======================================================================
# Validation-aware filtering helpers
# ======================================================================

def _filter_approved(
    prices_by_book: dict[str, int],
    validation_map: dict[str, str] | None = None,
) -> dict[str, int]:
    """Filter a prices dict to only include books with approved statuses.

    Parameters
    ----------
    prices_by_book : dict[str, int]
        ``{sportsbook: american_odds}``
    validation_map : dict[str, str] or None
        ``{sportsbook: validation_status}``.  If None, all records pass.

    Returns
    -------
    dict[str, int]
        Filtered dict with only approved records.
    """
    if validation_map is None:
        return prices_by_book

    return {
        book: price
        for book, price in prices_by_book.items()
        if validation_map.get(book, "") in APPROVED_STATUSES
    }


# ======================================================================
# Per-book market analysis
# ======================================================================

def analyze_side(
    prices_by_book: dict[str, int],
    validation_map: dict[str, str] | None = None,
) -> dict:
    """Analyze one side of a market (e.g. all away moneyline prices).

    Parameters
    ----------
    prices_by_book : dict[str, int]
        ``{sportsbook_name: american_odds}``
    validation_map : dict[str, str] or None
        Per-book validation statuses.  Only approved records are analysed.

    Returns
    -------
    dict with keys:
        n_books, consensus_price, consensus_decimal, best_price,
        best_book, worst_price, worst_book, std_dev, disagreement
    """
    prices = _filter_approved(prices_by_book, validation_map)

    if not prices:
        return {
            "n_books": 0, "consensus_price": 0, "consensus_decimal": 0.0,
            "best_price": 0, "best_book": None,
            "worst_price": 0, "worst_book": None,
            "std_dev": 0.0, "disagreement": 0.0,
        }

    vals = list(prices.values())
    n = len(vals)
    cons = consensus_price(vals)
    cons_dec = american_to_decimal(cons)

    decs = [american_to_decimal(p) for p in vals]
    sd = stdev(decs) if len(decs) >= 2 else 0.0

    best = best_price(vals)
    worst = worst_price(vals)

    best_book = max(prices, key=lambda b: american_to_decimal(prices[b]))
    worst_book = min(prices, key=lambda b: american_to_decimal(prices[b]))

    disagreement = american_to_decimal(best) - american_to_decimal(worst)

    return {
        "n_books": n,
        "consensus_price": cons,
        "consensus_decimal": round(cons_dec, 4),
        "best_price": best,
        "best_book": best_book,
        "worst_price": worst,
        "worst_book": worst_book,
        "std_dev": round(sd, 6),
        "disagreement": round(disagreement, 4),
    }


def analyze_two_way_market(
    side_a_prices: dict[str, int],
    side_b_prices: dict[str, int],
    label_a: str = "away",
    label_b: str = "home",
    validation_map_a: dict[str, str] | None = None,
    validation_map_b: dict[str, str] | None = None,
) -> dict:
    """Full analysis of a two-sided market with EV for every book.

    Only approved records (per validation_map) contribute to consensus,
    no-vig probability, and EV calculations.

    Parameters
    ----------
    side_a_prices, side_b_prices : dict[str, int]
        Prices indexed by sportsbook name.
    label_a, label_b : str
        Display labels for each side.
    validation_map_a, validation_map_b : dict[str, str] or None
        Per-book validation statuses.  Only APPROVED_STATUSES are used.

    Returns
    -------
    dict with keys:
        side_a, side_b: analyze_side output
        nv_prob_a, nv_prob_b: no-vig fair probabilities
        vig_pct: market vig percentage
        books: list of per-book EV and price info (only approved records)
        best_ev: the single best EV opportunity found
    """
    filtered_a = _filter_approved(side_a_prices, validation_map_a)
    filtered_b = _filter_approved(side_b_prices, validation_map_b)

    side_a = analyze_side(filtered_a)
    side_b = analyze_side(filtered_b)

    # No-vig from consensus (only approved records)
    nv_prob_a, nv_prob_b = remove_vig(
        side_a["consensus_price"],
        side_b["consensus_price"],
    )
    vig_pct = vig_percentage(side_a["consensus_price"], side_b["consensus_price"])

    # Per-book EV (only approved records)
    books = []
    for book, price in filtered_a.items():
        ev = expected_value(nv_prob_a, price)
        books.append({
            "sportsbook": book,
            "side": label_a,
            "american": price,
            "decimal": round(american_to_decimal(price), 4),
            "implied_prob": round(american_to_probability(price), 6),
            "fair_prob": round(nv_prob_a, 6),
            "ev": round(ev, 6),
            "validation_status": validation_map_a.get(book, "UNKNOWN") if validation_map_a else "VALID",
        })
    for book, price in filtered_b.items():
        ev = expected_value(nv_prob_b, price)
        books.append({
            "sportsbook": book,
            "side": label_b,
            "american": price,
            "decimal": round(american_to_decimal(price), 4),
            "implied_prob": round(american_to_probability(price), 6),
            "fair_prob": round(nv_prob_b, 6),
            "ev": round(ev, 6),
            "validation_status": validation_map_b.get(book, "UNKNOWN") if validation_map_b else "VALID",
        })

    books.sort(key=lambda b: b["ev"], reverse=True)

    best_ev = books[0] if books and books[0]["ev"] > 0 else None

    return {
        "side_a": side_a,
        "side_b": side_b,
        "label_a": label_a,
        "label_b": label_b,
        "nv_prob_a": round(nv_prob_a, 6),
        "nv_prob_b": round(nv_prob_b, 6),
        "vig_pct": round(vig_pct, 4),
        "books": books,
        "best_ev": best_ev,
    }


# ======================================================================
# CLV
# ======================================================================

def compute_clv(opening_price: int, closing_price: int) -> float:
    """Compute Closing Line Value (CLV).

    CLV = decimal(closing) - decimal(opening)

    Positive CLV means the price moved in your favour.
    """
    open_dec = american_to_decimal(opening_price)
    close_dec = american_to_decimal(closing_price)
    return round(close_dec - open_dec, 4)


# ======================================================================
# Slow-book detection
# ======================================================================

def find_slow_books(
    morning_prices: dict[str, int],
    pregame_prices: dict[str, int],
) -> list[dict]:
    """Identify sportsbooks that moved less than the market.

    Returns a list sorted by least movement first.
    """
    if not morning_prices or not pregame_prices:
        return []

    morning_cons = consensus_price(list(morning_prices.values()))
    pregame_cons = consensus_price(list(pregame_prices.values()))
    market_move = american_to_decimal(pregame_cons) - american_to_decimal(morning_cons)

    results = []
    for book in set(morning_prices) & set(pregame_prices):
        morning = morning_prices[book]
        pregame = pregame_prices[book]
        book_move = american_to_decimal(pregame) - american_to_decimal(morning)

        results.append({
            "sportsbook": book,
            "morning_price": morning,
            "pregame_price": pregame,
            "book_move_dec": round(book_move, 4),
            "market_move_dec": round(market_move, 4),
            "lag": round(abs(book_move) - abs(market_move), 4),
        })

    results.sort(key=lambda r: abs(r["book_move_dec"]))
    return results
