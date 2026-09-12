"""Execution-layer CLI.

    python -m src.execution.cli check-connectivity

Checks health_check() for every enabled provider and nothing else --
never calls get_balance, get_markets, or any write endpoint directly.
Safe to run against real production credentials.
"""

from __future__ import annotations

import argparse
import sys

from src.execution import get_provider
from src.production_config import load_config


def _check_connectivity() -> int:
    config = load_config()
    providers = [
        ("kalshi", config.kalshi_enabled),
        ("polymarket_us", config.polymarket_us_enabled),
    ]

    any_failed = False
    for name, enabled in providers:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.execution.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-connectivity", help="Health-check every enabled provider (read-only)")

    args = parser.parse_args(argv)
    if args.command == "check-connectivity":
        return _check_connectivity()
    return 1


if __name__ == "__main__":
    sys.exit(main())
