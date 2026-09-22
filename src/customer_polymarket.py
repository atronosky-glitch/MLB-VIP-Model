"""Per-customer Polymarket Auto-Bet connection (2026-09-21).

Lets a logged-in customer (see src/customer_accounts.py) connect their
OWN Polymarket US account -- API Key ID + Ed25519 private key, the
exact two credentials src/execution/polymarket_us.py's
PolymarketUSProvider actually requires, confirmed by reading that
provider directly rather than guessed. Verification uses the real
provider's health_check() (a harmless authenticated GET,
/v1/account/balances -- never an order). Credentials are encrypted at
rest (src/credential_encryption.py, Fernet, server-side master key)
and decrypted only in memory, only when a caller needs them for a
verify or execution attempt -- never returned from any function here,
never logged.

Auto-Bet (autobet_enabled) always defaults OFF and connecting an
account never turns it on by itself -- see enable_autobet, which also
requires risk settings to already be configured. live_execution
(PAPER vs LIVE) is a SEPARATE flag, also default OFF: even once both
are true for a customer, src/execution/customer_autobet.py still
re-checks the SAME global config gates
(config.live_trading_enabled / config.polymarket_us_live_enabled) the
existing single-operator live-execution path already enforces -- a
customer's own settings are necessary, never sufficient, for a real
order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.credential_encryption import (
    CredentialEncryptionError,
    credential_set_fingerprint,
    decrypt_secret,
    encrypt_secret,
    fingerprint_secret,
)
from src.execution.credentials import CredentialLoadError
from src.execution.polymarket_us import PolymarketUSProvider

logger = logging.getLogger(__name__)

# Conservative starting defaults for a customer who hasn't configured
# their own risk settings yet -- shown as pre-filled suggestions in the
# UI, never silently applied without the customer seeing/saving them.
DEFAULT_RISK_SETTINGS = {
    "unit_size_usd": 5.0,
    "max_bet_usd": 25.0,
    "max_daily_loss_usd": 50.0,
    "max_total_exposure_usd": 200.0,
    "max_open_positions": 10,
    "min_net_ev_pct": 2.0,
    "max_price_move_pct": 2.0,
    "max_slippage_pct": 5.0,
    "auto_approve_max_usd": 10.0,
}

_RISK_SETTING_FIELDS = (
    "unit_size_usd", "max_bet_usd", "max_daily_loss_usd", "max_total_exposure_usd",
    "max_open_positions", "min_net_ev_pct", "max_price_move_pct", "max_slippage_pct",
    "auto_approve_max_usd", "sport_filter",
)

# Must all be configured (non-null) before Auto-Bet can be enabled --
# sport_filter and the two price/slippage tolerances are optional, the
# rest are the core exposure/loss bounds a customer must deliberately
# set (Requirement section 6's pre-flight list).
_REQUIRED_FOR_AUTOBET = (
    "unit_size_usd", "max_bet_usd", "max_daily_loss_usd", "max_total_exposure_usd",
    "max_open_positions", "min_net_ev_pct",
)


@dataclass(frozen=True)
class PolymarketAccountStatus:
    connected: bool
    api_key_id_display: str | None
    connected_at: str | None
    last_verified_at: str | None
    last_verify_status: str | None
    last_verify_error: str | None
    autobet_enabled: bool
    live_execution: bool


def get_status(conn, account_id: str) -> PolymarketAccountStatus:
    """Never includes decrypted credentials -- only a masked display
    string built from the stored fingerprint."""
    row = conn.execute(
        "SELECT * FROM customer_polymarket_accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return PolymarketAccountStatus(False, None, None, None, None, None, False, False)
    d = dict(row)
    fp = d.get("api_key_id_fingerprint")
    return PolymarketAccountStatus(
        connected=bool(d["polymarket_connected"]),
        api_key_id_display=(f"••••••••{fp}" if fp else None),
        connected_at=d.get("connected_at"),
        last_verified_at=d.get("last_verified_at"),
        last_verify_status=d.get("last_verify_status"),
        last_verify_error=d.get("last_verify_error"),
        autobet_enabled=bool(d["autobet_enabled"]),
        live_execution=bool(d["live_execution"]),
    )


def verify_credentials(api_key_id: str, private_key_b64: str) -> tuple[bool, str]:
    """Authenticate against the REAL Polymarket US client with a
    harmless, read-only, authenticated request (account balances) --
    never places an order. Returns (ok, message); message is always
    safe to show the customer directly -- never the raw provider
    exception (health_check()'s own `detail` field can include a raw
    exception string, which is server-log material, not customer-
    facing text)."""
    if not api_key_id or not private_key_b64:
        return False, "API Key and Private Key are both required."
    try:
        provider = PolymarketUSProvider(api_key_id=api_key_id, private_key_b64=private_key_b64)
    except CredentialLoadError:
        return False, "That Private Key doesn't look valid -- check that you pasted the whole value."

    try:
        result = provider.health_check()
    except Exception:
        logger.exception("Polymarket US verify_credentials health_check raised unexpectedly")
        return False, "Could not reach Polymarket right now -- please try again in a moment."

    if not result.ok:
        logger.info("Polymarket US credential verification failed for a customer (detail withheld from customer-facing message)")
        return False, "Polymarket rejected those credentials. Double-check your API Key and Private Key."
    return True, "Connected successfully."


def connect_account(conn, account_id: str, api_key_id: str, private_key_b64: str) -> tuple[bool, str]:
    """Verify first; only on success are credentials encrypted and
    saved. Auto-Bet is always left OFF here, including on a
    Replace-Credentials call -- rotating credentials never carries
    forward a prior enabled state, so the customer must deliberately
    re-enable Auto-Bet after confirming the new credentials work."""
    ok, message = verify_credentials(api_key_id, private_key_b64)
    now = datetime.now(timezone.utc).isoformat()

    if not ok:
        _upsert_verify_only(conn, account_id, now, "FAILED", message)
        return False, message

    try:
        encrypted_api_key_id = encrypt_secret(api_key_id)
        encrypted_private_key = encrypt_secret(private_key_b64)
    except CredentialEncryptionError:
        logger.exception("Could not encrypt customer Polymarket credentials")
        return False, "Could not save your credentials right now -- please try again in a moment."

    api_fp = fingerprint_secret(api_key_id)
    cred_fp = credential_set_fingerprint(api_key_id, private_key_b64)

    conn.execute(
        """INSERT INTO customer_polymarket_accounts (
               account_id, polymarket_connected, encrypted_api_key_id, encrypted_private_key,
               api_key_id_fingerprint, credential_fingerprint, connected_at, last_verified_at,
               last_verify_status, last_verify_error, autobet_enabled, live_execution, updated_at
           ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'OK', NULL, 0, 0, ?)
           ON CONFLICT (account_id) DO UPDATE SET
               polymarket_connected = 1,
               encrypted_api_key_id = excluded.encrypted_api_key_id,
               encrypted_private_key = excluded.encrypted_private_key,
               api_key_id_fingerprint = excluded.api_key_id_fingerprint,
               credential_fingerprint = excluded.credential_fingerprint,
               connected_at = excluded.connected_at,
               last_verified_at = excluded.last_verified_at,
               last_verify_status = 'OK',
               last_verify_error = NULL,
               autobet_enabled = 0,
               updated_at = excluded.updated_at""",
        (account_id, encrypted_api_key_id, encrypted_private_key, api_fp, cred_fp, now, now, now),
    )
    conn.commit()
    return True, "Polymarket account connected."


def _upsert_verify_only(conn, account_id: str, now: str, status: str, error: str) -> None:
    """Record a failed verify attempt without touching any existing
    stored credentials (a failed Replace-Credentials attempt must never
    disturb a working, already-connected account)."""
    existing = conn.execute(
        "SELECT account_id FROM customer_polymarket_accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE customer_polymarket_accounts
               SET last_verified_at = ?, last_verify_status = ?, last_verify_error = ?, updated_at = ?
               WHERE account_id = ?""",
            (now, status, error, now, account_id),
        )
    else:
        conn.execute(
            """INSERT INTO customer_polymarket_accounts (
                   account_id, polymarket_connected, last_verified_at, last_verify_status,
                   last_verify_error, autobet_enabled, live_execution
               ) VALUES (?, 0, ?, ?, ?, 0, 0)""",
            (account_id, now, status, error),
        )
    conn.commit()


def disconnect_account(conn, account_id: str) -> None:
    """Wipes stored credentials and turns Auto-Bet off. Existing
    positions are NOT touched -- disconnecting only stops FUTURE
    orders (same principle as the PAUSE AUTO-BET kill switch)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE customer_polymarket_accounts SET
               polymarket_connected = 0, encrypted_api_key_id = NULL, encrypted_private_key = NULL,
               api_key_id_fingerprint = NULL, credential_fingerprint = NULL,
               autobet_enabled = 0, live_execution = 0, updated_at = ?
           WHERE account_id = ?""",
        (now, account_id),
    )
    conn.commit()


def get_decrypted_credentials(conn, account_id: str) -> tuple[str, str] | None:
    """Decrypt this customer's stored credentials server-side, for the
    exact moment src/execution/customer_autobet.py needs them to build
    a provider instance. Returns None if not connected. Callers must
    never log, print, or persist the returned values -- hold them only
    long enough to construct a PolymarketUSProvider."""
    row = conn.execute(
        "SELECT polymarket_connected, encrypted_api_key_id, encrypted_private_key "
        "FROM customer_polymarket_accounts WHERE account_id = ?", (account_id,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    if not d["polymarket_connected"] or not d["encrypted_api_key_id"] or not d["encrypted_private_key"]:
        return None
    api_key_id = decrypt_secret(d["encrypted_api_key_id"])
    private_key_b64 = decrypt_secret(d["encrypted_private_key"])
    return api_key_id, private_key_b64


def enable_autobet(conn, account_id: str) -> tuple[bool, str]:
    """Turn Auto-Bet ON -- only once credentials are connected and the
    required risk settings are configured. Global live-execution health
    / kill-switch state are re-checked at EXECUTION time regardless
    (see src/execution/customer_autobet.py), not just here, since those
    can change after this call returns."""
    row = conn.execute(
        "SELECT * FROM customer_polymarket_accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is None or not dict(row)["polymarket_connected"]:
        return False, "Connect your Polymarket account first."
    d = dict(row)
    missing = [f for f in _REQUIRED_FOR_AUTOBET if d.get(f) is None]
    if missing:
        return False, "Configure your risk settings before enabling Auto-Bet."
    conn.execute(
        "UPDATE customer_polymarket_accounts SET autobet_enabled = 1, updated_at = ? WHERE account_id = ?",
        (datetime.now(timezone.utc).isoformat(), account_id),
    )
    conn.commit()
    return True, "Auto-Bet enabled."


def disable_autobet(conn, account_id: str) -> None:
    """PAUSE AUTO-BET -- stops all future automatic orders immediately
    (src/execution/customer_autobet.py checks this flag fresh, right
    before every attempt). Never touches existing open positions."""
    conn.execute(
        "UPDATE customer_polymarket_accounts SET autobet_enabled = 0, updated_at = ? WHERE account_id = ?",
        (datetime.now(timezone.utc).isoformat(), account_id),
    )
    conn.commit()


def set_live_execution(conn, account_id: str, live: bool) -> None:
    """Switch a connected account between PAPER (default) and LIVE.
    This alone is never sufficient to place a real order -- see this
    module's own docstring for the global config gates that still
    apply on top."""
    conn.execute(
        "UPDATE customer_polymarket_accounts SET live_execution = ?, updated_at = ? WHERE account_id = ?",
        (1 if live else 0, datetime.now(timezone.utc).isoformat(), account_id),
    )
    conn.commit()


def get_risk_settings(conn, account_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM customer_polymarket_accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if row is None:
        return {f: None for f in _RISK_SETTING_FIELDS}
    d = dict(row)
    return {f: d.get(f) for f in _RISK_SETTING_FIELDS}


def save_risk_settings(conn, account_id: str, **settings) -> None:
    """Only the fields in _RISK_SETTING_FIELDS are accepted; anything
    else raises rather than being silently ignored (catches a typo'd
    kwarg immediately instead of it quietly never taking effect)."""
    unknown = set(settings) - set(_RISK_SETTING_FIELDS)
    if unknown:
        raise ValueError(f"unknown risk setting field(s): {sorted(unknown)}")
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT account_id FROM customer_polymarket_accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    if existing is None:
        raise ValueError("connect a Polymarket account before saving risk settings")

    set_clause = ", ".join(f"{f} = ?" for f in settings)
    values = [settings[f] for f in settings]
    conn.execute(
        f"UPDATE customer_polymarket_accounts SET {set_clause}, updated_at = ? WHERE account_id = ?",
        (*values, now, account_id),
    )
    conn.commit()
