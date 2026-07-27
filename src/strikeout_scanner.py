"""Backward-compatible strikeout scanner wrapper.

Delegates to :mod:`src.player_prop_scanner` with market forced to
``"strikeouts"``.  All existing CLI flags and output formats are
preserved.

Usage (unchanged)::

    python -m src.strikeout_scanner [--all] [--positive-only] [--actionable-only]
                                     [--min-ev PCT] [--limit N]
                                     [--market ou|yn|all]
"""

from __future__ import annotations

import argparse
import sys

from . import prop_config as cfg
from . import player_prop_scanner as _generic


def run_scan(
    mode: str = "actionable",
    min_ev: float | None = None,
    limit: int = 25,
    market: str = "all",
) -> dict:
    """Run the scanner pipeline for strikeouts only.

    Thin wrapper around :func:`player_prop_scanner.run_scan`.

    Parameters
    ----------
    mode, min_ev, limit : see :mod:`player_prop_scanner`.
    market : str
        ``"ou"`` (over/under only), ``"yn"`` (yes/no only),
        or ``"all"`` (both).
    """
    # Map old --market ou|yn|all to the new market_form
    market_form = market  # "ou", "yn", or "all" maps directly
    return _generic.run_scan(
        mode=mode,
        min_ev=min_ev,
        limit=limit,
        market="strikeouts",
        market_form=market_form,
    )


def display_results(result: dict, mode: str) -> None:
    """Print scanner results — delegates to generic scanner."""
    _generic.display_results(result, mode)


def display_verbose(result: dict) -> None:
    """Print detailed per-opportunity info — delegates to generic scanner."""
    _generic.display_verbose(result)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args — identical interface to the original strikeout scanner."""
    parser = argparse.ArgumentParser(
        description="MLB Pitcher Strikeout Edge Scanner",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true",
                      help="Show all market sides (including NO_EDGE, research-only)")
    mode.add_argument("--positive-only", action="store_true",
                      help="Show only recommendation-eligible positive EV sides")
    mode.add_argument("--actionable-only", action="store_true",
                      help="Show only recommendation-eligible sides above actionable threshold (default)")
    parser.add_argument("--min-ev", type=float, default=None,
                        help=f"Override actionable EV minimum (default "
                             f"{cfg.ACTIONABLE_EDGE_THRESHOLD:.0%})")
    parser.add_argument("--limit", type=int, default=25,
                        help="Max opportunities to display (default 25)")
    parser.add_argument("--market", choices=["ou", "yn", "all"], default="all",
                        help="Filter by market type: ou (over/under), yn (yes/no), all (default)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show detailed per-opportunity output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

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

    if min_ev is not None and args.market == "yn":
        print("ERROR: --min-ev cannot be used with --market yn. "
              "EV is not computed for Yes/No markets (no complementary price).",
              file=sys.stderr)
        sys.exit(1)

    result = run_scan(mode=mode, min_ev=min_ev, limit=args.limit, market=args.market)

    if args.verbose and result["opportunities"]:
        display_verbose(result)
    else:
        display_results(result, mode)


if __name__ == "__main__":
    main()
