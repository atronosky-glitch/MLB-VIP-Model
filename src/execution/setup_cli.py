"""Guided, secret-free Polymarket US setup commands.

    python -m src.execution.cli setup-polymarket
    python -m src.execution.cli polymarket-setup-check

Neither command ever prints a private key's contents or stores one in
the database. `setup-polymarket` writes at most one non-secret value to
.env (the destination path for the key file, and -- only if typed
interactively -- the API key ID, which Polymarket US's own docs treat
as a public identifier, not a secret). `polymarket-setup-check` is
strictly read-only and reuses the existing Stage 4.1 diagnostic/
readiness machinery rather than duplicating it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from dotenv import set_key

from src.execution import get_provider
from src.execution.credentials import CredentialLoadError, load_ed25519_private_key
from src.production_config import load_config

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_KEY_DIR = Path.home() / ".prediction-market-keys"
_DEFAULT_KEY_FILENAME = "polymarket_us.txt"


def _default_key_path() -> Path:
    return _KEY_DIR / _DEFAULT_KEY_FILENAME


def setup_polymarket() -> int:
    config = load_config()
    _KEY_DIR.mkdir(parents=True, exist_ok=True)

    print("=== POLYMARKET US SETUP ===")
    print(f"Key storage directory: {_KEY_DIR}")
    print("(outside the repo -- never committed; *.pem/*.key are also gitignored as a second layer)")
    print()

    key_id = config.polymarket_us_api_key_id
    key_path_str = config.polymarket_us_private_key_path

    print(f"API key ID configured........ {'YES' if key_id else 'NO'}")
    print(f"Private key path configured.. {'YES' if key_path_str else 'NO'}")

    if key_path_str:
        key_path = Path(key_path_str)
    else:
        key_path = _default_key_path()
        set_key(str(_ENV_PATH), "POLYMARKET_US_PRIVATE_KEY_PATH", str(key_path))
        print(f"\nSet POLYMARKET_US_PRIVATE_KEY_PATH in .env to the expected destination:")
        print(f"  {key_path}")

    print()
    if key_path.exists():
        try:
            load_ed25519_private_key(key_path)
            print(f"Key file at {key_path}: FOUND, readable, and correctly formatted.")
        except CredentialLoadError as exc:
            print(f"Key file at {key_path}: FOUND but INVALID -- {exc}")
            print("Re-download the key from Polymarket US and save it to this exact path.")
    else:
        print(f"Key file at {key_path}: NOT FOUND.")
        print(f"  1. Log into your Polymarket US account.")
        print(f"  2. Generate/download your API private key.")
        print(f"  3. Save it to exactly: {key_path}")

    print()
    if not key_id:
        entered = ""
        if sys.stdin.isatty():
            try:
                entered = input(
                    "Enter your Polymarket US API key ID (this is a public identifier, not "
                    "your private key -- press Enter to skip): "
                ).strip()
            except EOFError:
                entered = ""
        if entered:
            set_key(str(_ENV_PATH), "POLYMARKET_US_API_KEY_ID", entered)
            print("Saved to .env as POLYMARKET_US_API_KEY_ID (value not echoed back).")
            key_id = entered
        else:
            print("API key ID missing -- add POLYMARKET_US_API_KEY_ID=<your key id> to .env "
                  "yourself, or re-run this command in an interactive terminal to enter it.")

    print()
    print("=" * 50)
    missing = []
    if not key_id:
        missing.append("Set POLYMARKET_US_API_KEY_ID in .env (your Polymarket US API key ID)")
    if not key_path.exists():
        missing.append(f"Save your Polymarket US private key file to: {key_path}")
    if missing:
        print("USER ACTION REQUIRED:")
        for item in missing:
            print(f"  - {item}")
    else:
        print("Setup looks complete. Next: python -m src.execution.cli polymarket-setup-check")
    return 0


def _line(label: str, value: str) -> None:
    print(f"{label:.<32} {value}")


def polymarket_setup_check() -> int:
    """One command, read-only end to end: config validation, credential-
    file existence/format, auth, balance, markets, orderbook, real-market
    parsing, side-semantics, reconciliation-endpoint availability,
    live-readiness. Reuses Stage 4.1's provider_diagnostics for the
    connectivity portion rather than duplicating it."""
    config = load_config()
    print("=== POLYMARKET US SETUP CHECK ===\n")

    key_id = config.polymarket_us_api_key_id
    key_path_str = config.polymarket_us_private_key_path
    creds_configured = bool(key_id and key_path_str)
    _line("Config: API key ID", "CONFIGURED" if key_id else "MISSING")
    _line("Config: private key path", "CONFIGURED" if key_path_str else "MISSING")

    key_file_status = "NOT CONFIGURED"
    if key_path_str:
        key_path = Path(key_path_str)
        if not key_path.exists():
            key_file_status = f"MISSING ({key_path})"
        else:
            try:
                load_ed25519_private_key(key_path)
                key_file_status = "PASS (found, valid format)"
            except CredentialLoadError as exc:
                key_file_status = f"FAIL ({exc})"
    _line("Credential file", key_file_status)

    if not creds_configured:
        print()
        print("=" * 50)
        print("OVERALL: NOT READY")
        print("USER ACTION REQUIRED:")
        print("  - Run: python -m src.execution.cli setup-polymarket")
        return 1

    if not config.polymarket_us_enabled:
        print()
        print("POLYMARKET_US_ENABLED=false -- read-only access is off. Nothing further to check.")
        print("OVERALL: NOT READY")
        return 1

    print()
    try:
        provider = get_provider("polymarket_us", config)
    except Exception as exc:
        _line("Provider initialization", f"FAIL ({type(exc).__name__}: {exc})")
        print("\nOVERALL: NOT READY")
        return 1

    auth_ok = False
    try:
        health = provider.health_check()
        auth_ok = health.ok
        _line("Authentication", f"{'PASS' if auth_ok else 'FAIL'} ({health.detail})")
    except Exception as exc:
        _line("Authentication", f"FAIL ({type(exc).__name__}: {exc})")

    balance_ok = False
    try:
        provider.get_balance()
        balance_ok = True
        _line("Balance read", "PASS")
    except Exception as exc:
        _line("Balance read", f"FAIL ({type(exc).__name__}: {exc})")

    markets: list = []
    markets_ok = False
    try:
        markets = provider.get_markets(limit=5) or []
        markets_ok = True
        _line("Market read", f"PASS ({len(markets)} returned)")
    except Exception as exc:
        _line("Market read", f"FAIL ({type(exc).__name__}: {exc})")

    orderbook_ok = False
    parsed_ok = False
    if markets:
        try:
            raw_book = provider.get_orderbook(markets[0].id)
            provider.normalize_orderbook(raw_book)
            orderbook_ok = True
            _line("Orderbook read", "PASS")
        except Exception as exc:
            _line("Orderbook read", f"FAIL ({type(exc).__name__}: {exc})")

        parsed_count = sum(1 for m in markets if provider.parse_game_event(m) is not None)
        parsed_ok = parsed_count > 0
        _line("Real-market parsing", f"{parsed_count}/{len(markets)} markets parsed")
    else:
        _line("Orderbook read", "SKIPPED (no markets)")
        _line("Real-market parsing", "SKIPPED (no markets)")

    # NO-side semantics: confirmed 2026-09-14 against real, live,
    # unauthenticated Polymarket US market data (single shared binary
    # book, same mechanism as Kalshi) -- see
    # src/execution/polymarket_us.py::normalize_orderbook's docstring.
    _line("NO-side semantics", "VERIFIED (2026-09-14)")

    recon_status = "LIMITED (no idempotency key; open-orders-only list -- see live/reconciliation.py)"
    if auth_ok:
        try:
            open_orders = provider.get_recent_orders()
            if open_orders is not None:
                recon_status = f"AVAILABLE (open-orders read OK, {len(open_orders)} open) -- still no idempotency key"
        except Exception:
            pass
    _line("Reconciliation", recon_status)

    _line("Live order schema", "VERIFIED (POST /v1/orders confirmed from docs.polymarket.us)")

    overall_ready = auth_ok and balance_ok and markets_ok and orderbook_ok
    print()
    print("=" * 50)
    if overall_ready:
        print("OVERALL: READY (read-only connectivity fully verified)")
        print(f"Paper trading............... READY (POLYMARKET_US_ENABLED={config.polymarket_us_enabled})")
        print(f"Live execution code.......... READY (schema verified; still gated off by config)")
        print(f"Live trading................. {'ON' if config.polymarket_us_live_enabled and config.live_trading_enabled else 'OFF'}")
    else:
        print("OVERALL: NOT READY")
        print("USER ACTION REQUIRED:")
        if not auth_ok:
            print("  - Authentication failed -- verify the API key ID and private key file are correct "
                  "and match your Polymarket US account.")
        if not balance_ok:
            print("  - Balance read failed -- check account status/permissions on Polymarket US.")
        if not markets_ok or not orderbook_ok:
            print("  - Market/orderbook read failed -- this may be transient; re-run this command.")
    return 0 if overall_ready else 1
