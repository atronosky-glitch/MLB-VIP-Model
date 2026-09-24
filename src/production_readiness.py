"""Production readiness report: ``python -m src.production_readiness``.

One command that answers "can real customers use this right now, and is the
backend healthy without me watching it?". Prints PASS / WARN / FAIL per
subsystem and exits 1 if anything FAILS (0 otherwise).

Safety: strictly READ-ONLY. It never places, approves, cancels or even
prepares an order, never sends a Discord/e-mail message, never writes
customer data, and never prints a secret (database URLs are reduced to
host/db; keys are only checked for presence/validity).

Options: ``--skip-network`` (no provider connectivity probes), ``--json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# thresholds
HEARTBEAT_MAX_AGE_S = 300
MODEL_RUN_MAX_AGE_H = 30          # a daily scan must have finished within ~a day
NETWORK_TIMEOUT_S = 8


@dataclass
class Check:
    subsystem: str
    status: str
    message: str
    details: dict = field(default_factory=dict)


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return None


def _is_production() -> bool:
    return os.environ.get("MLB_ENVIRONMENT", "").strip().lower() == "production"


# ── individual checks (each never raises) ───────────────────────────


def check_config(config) -> Check:
    try:
        errors = config.validate()
    except Exception as exc:
        return Check("config", FAIL, f"configuration could not be validated: {type(exc).__name__}")
    if errors:
        return Check("config", FAIL, f"{len(errors)} configuration error(s)", {"errors": errors[:10]})
    return Check("config", PASS, "configuration valid")


def check_database(conn) -> Check:
    from database.connection import describe_database_backend

    backend = describe_database_backend()
    try:
        conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        return Check("database", FAIL, f"cannot query the database ({type(exc).__name__})", {"backend": backend})
    if _is_production() and not os.environ.get("DATABASE_URL"):
        return Check("database", FAIL, "production is using local SQLite (DATABASE_URL not set)", {"backend": backend})
    return Check("database", PASS, f"connected: {backend}", {"backend": backend})


def check_schema(conn) -> Check:
    from database.db_manager import verify_required_schema

    try:
        verify_required_schema(conn)
    except Exception as exc:
        return Check("schema", FAIL, f"required tables missing: {str(exc)[:200]}")
    needed = {
        "customer_accounts": ("disabled", "password_reset_token_hash"),
        "customer_autobet_executions": ("requested_quantity", "fees_usd", "fill_source"),
    }
    missing = []
    for table, columns in needed.items():
        try:
            conn.execute(f"SELECT {', '.join(columns)} FROM {table} LIMIT 1").fetchall()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            missing.append(table)
    if missing:
        return Check("schema", FAIL, f"migrations not applied for: {', '.join(missing)} (restart a service to run init_db)")
    return Check("schema", PASS, "required tables and launch migrations present")


def check_worker_heartbeat(conn) -> Check:
    try:
        row = _one(conn, "SELECT * FROM worker_heartbeat WHERE id = 1")
    except Exception:
        return Check("worker_heartbeat", FAIL, "worker_heartbeat table unreadable (worker never started?)")
    if not row:
        return Check("worker_heartbeat", FAIL, "no worker heartbeat recorded -- the worker is not running")
    age = _age_seconds(row.get("last_heartbeat"))
    details = {k: row.get(k) for k in ("worker_pid", "current_job", "rss_mb", "uptime_seconds",
                                       "last_success_job", "last_success_at", "last_failure_job",
                                       "last_failure_at") if row.get(k) is not None}
    if age is None or age > HEARTBEAT_MAX_AGE_S:
        return Check("worker_heartbeat", FAIL, f"worker heartbeat stale ({'unknown' if age is None else f'{age / 60:.0f}m'})", details)
    busy = f", running {row['current_job']}" if row.get("current_job") else ""
    return Check("worker_heartbeat", PASS, f"worker alive: heartbeat {age:.0f}s ago{busy}", details)


def check_model_runs(conn) -> Check:
    try:
        ok = _one(conn, "SELECT finished_at FROM scan_runs WHERE finished_at IS NOT NULL AND error_message IS NULL "
                        "ORDER BY finished_at DESC LIMIT 1")
        bad = _one(conn, "SELECT finished_at, error_message FROM scan_runs WHERE error_message IS NOT NULL "
                         "ORDER BY started_at DESC LIMIT 1")
    except Exception:
        return Check("model_runs", FAIL, "scan_runs unreadable")
    if not ok:
        return Check("model_runs", WARN, "no completed model run recorded yet")
    age_h = (_age_seconds(ok["finished_at"]) or 0) / 3600
    details = {"last_success_age_h": round(age_h, 1)}
    if bad:
        details["last_failure"] = str(bad.get("error_message"))[:200]
    if age_h > MODEL_RUN_MAX_AGE_H:
        return Check("model_runs", WARN, f"last successful model run was {age_h:.0f}h ago", details)
    return Check("model_runs", PASS, f"last successful model run {age_h:.1f}h ago", details)


def check_recommendations_fresh(conn) -> Check:
    try:
        latest = _one(conn, "SELECT MAX(selected_at) AS t FROM official_picks")
        upcoming = _one(conn, "SELECT COUNT(*) AS n FROM official_picks op JOIN historical_recommendations hr "
                              "ON hr.recommendation_id = op.recommendation_id "
                              "WHERE op.pick_status = 'ACTIVE' AND hr.event_start_time > ?",
                        (datetime.now(timezone.utc).isoformat(),))
    except Exception:
        return Check("official_picks", FAIL, "official_picks unreadable")
    n = (upcoming or {}).get("n", 0)
    age = _age_seconds((latest or {}).get("t"))
    if age is None:
        return Check("official_picks", WARN, "no official picks recorded yet", {"upcoming": n})
    return Check("official_picks", PASS, f"{n} upcoming official pick(s); newest selected {age / 3600:.1f}h ago",
                 {"upcoming": n})


def check_odds_data(conn) -> Check:
    try:
        row = _one(conn, "SELECT MAX(captured_at) AS t FROM player_prop_odds")
    except Exception:
        return Check("odds_data", WARN, "player_prop_odds unreadable")
    age = _age_seconds((row or {}).get("t"))
    if age is None:
        return Check("odds_data", WARN, "no odds captured yet")
    if age > 36 * 3600:
        return Check("odds_data", WARN, f"newest odds row is {age / 3600:.0f}h old")
    return Check("odds_data", PASS, f"newest odds row {age / 3600:.1f}h old")


def check_auth(conn) -> Check:
    try:
        import bcrypt
        h = bcrypt.hashpw(b"readiness-probe", bcrypt.gensalt(rounds=4))
        assert bcrypt.checkpw(b"readiness-probe", h)
        n = _one(conn, "SELECT COUNT(*) AS n FROM customer_accounts")["n"]
    except Exception as exc:
        return Check("customer_auth", FAIL, f"authentication stack not working ({type(exc).__name__})")
    admin = bool(os.environ.get("MLB_ADMIN_PASSWORD"))
    notes = []
    if _is_production() and not admin:
        return Check("customer_auth", FAIL, "MLB_ADMIN_PASSWORD not set: the admin dashboard is locked out",
                     {"accounts": n})
    if not (os.environ.get("SENDGRID_API_KEY") and os.environ.get("SENDGRID_FROM_EMAIL")
            and os.environ.get("SITE_BASE_URL")):
        notes.append("email not configured (SENDGRID_API_KEY/SENDGRID_FROM_EMAIL/SITE_BASE_URL): "
                     "verification and password-reset emails will NOT send")
    if os.environ.get("MLB_CUSTOMER_FREE_ACCESS", "true").strip().lower() != "false":
        notes.append("free-access mode: picks visible without an account (sign-up offered on the page)")
    status = WARN if any("NOT send" in x for x in notes) else PASS
    return Check("customer_auth", status, f"auth stack OK, {n} account(s)" + ("; " + "; ".join(notes) if notes else ""),
                 {"accounts": n, "admin_password_set": admin})


def check_credential_encryption() -> Check:
    try:
        from src.credential_encryption import decrypt_secret, encrypt_secret
        assert decrypt_secret(encrypt_secret("readiness-probe")) == "readiness-probe"
    except Exception as exc:
        return Check("credential_encryption", FAIL,
                     f"POLYMARKET_CREDENTIAL_ENCRYPTION_KEY missing/invalid: customers cannot connect platforms "
                     f"({type(exc).__name__})")
    return Check("credential_encryption", PASS, "encryption key valid (round-trip OK)")


def check_autobet(conn, config) -> Check:
    live = {"LIVE_TRADING_ENABLED": bool(config.live_trading_enabled),
            "KALSHI_LIVE_ENABLED": bool(config.kalshi_live_enabled),
            "POLYMARKET_US_LIVE_ENABLED": bool(config.polymarket_us_live_enabled)}
    details: dict[str, Any] = {"live_flags": live,
                               "kalshi_server_cap_usd": config.kalshi_autobet_server_max_order_usd,
                               "polymarket_server_cap_usd": config.polymarket_us_autobet_server_max_order_usd}
    try:
        from src.execution.live import kill_switch
        engaged, _reason = kill_switch.is_kill_switch_engaged(conn)
        details["kill_switch_engaged"] = bool(engaged)
        details["kalshi_accounts_connected"] = _one(conn, "SELECT COUNT(*) AS n FROM customer_kalshi_accounts WHERE kalshi_connected = 1")["n"]
        details["polymarket_accounts_connected"] = _one(conn, "SELECT COUNT(*) AS n FROM customer_polymarket_accounts WHERE polymarket_connected = 1")["n"]
        details["live_accounts_enabled"] = sum(
            _one(conn, f"SELECT COUNT(*) AS n FROM {t} WHERE live_execution = 1 AND autobet_enabled = 1")["n"]
            for t in ("customer_kalshi_accounts", "customer_polymarket_accounts"))
        details["unreconciled_live_kalshi_orders"] = _one(
            conn, "SELECT COUNT(*) AS n FROM customer_autobet_executions WHERE platform = 'kalshi' AND mode = 'LIVE' "
                  "AND status IN ('EXECUTED','PARTIALLY_FILLED') AND (fill_source IS NULL OR fill_source <> 'PLATFORM_FILLS')")["n"]
    except Exception as exc:
        return Check("autobet", FAIL, f"Auto-Bet state unreadable ({type(exc).__name__})", details)
    if any(live.values()):
        on = ", ".join(k for k, v in live.items() if v)
        return Check("autobet", WARN, f"LIVE execution gates ON: {on} (intended only during a controlled live test)", details)
    return Check("autobet", PASS, "live execution gates OFF (paper only); server caps set", details)


def check_discord(config) -> Check:
    def n(value: str) -> int:
        return len([u for u in (value or "").split(",") if u.strip()])

    ev, arb, mid = (n(config.discord_webhook_urls), n(config.discord_webhook_urls_arb_middle),
                    n(config.discord_webhook_urls_middle))
    details = {"ev_webhooks": ev, "arb_webhooks": arb, "middle_webhooks": mid}
    if not (ev or arb or mid):
        return Check("discord", WARN, "no Discord webhooks configured (alerts will not be sent)", details)
    return Check("discord", PASS, f"webhooks configured (ev={ev}, arb={arb}, middle={mid}); readiness never sends", details)


def check_backups(conn) -> Check:
    if os.environ.get("DATABASE_URL"):
        return Check("backups", WARN,
                     "PostgreSQL in production: the app's SQLite backup job does not apply. Backups depend on the "
                     "Render database plan -- verify in the Render dashboard (Database > Backups) that automatic "
                     "backups are on and test a restore. NOT verifiable from code.")
    return Check("backups", PASS, "local SQLite mode")


def check_providers(skip_network: bool) -> list[Check]:
    if skip_network:
        return [Check("providers", WARN, "network probes skipped (--skip-network)")]
    import requests

    checks: list[Check] = []
    key = os.environ.get("SPORTSODDS_API_KEY", "")
    if not key:
        checks.append(Check("provider_sportsgameodds", FAIL, "SPORTSODDS_API_KEY not set"))
    else:
        try:
            r = requests.get("https://api.sportsgameodds.com/v2/account/usage", headers={"x-api-key": key},
                             timeout=NETWORK_TIMEOUT_S)
            if r.status_code != 200:
                checks.append(Check("provider_sportsgameodds", FAIL, f"usage endpoint HTTP {r.status_code}"))
            else:
                month = (r.json().get("data") or {}).get("rateLimits", {}).get("per-month", {})
                limit, used = month.get("max-entities"), month.get("current-entities")
                if isinstance(limit, (int, float)) and isinstance(used, (int, float)) and used >= limit:
                    checks.append(Check("provider_sportsgameodds", WARN,
                                        f"monthly entity quota exhausted ({used}/{limit}); MLB/NFL use the Odds API "
                                        f"fallback for game markets only", {"used": used, "limit": limit}))
                else:
                    checks.append(Check("provider_sportsgameodds", PASS, f"reachable; monthly entities {used}/{limit}"))
        except Exception as exc:
            checks.append(Check("provider_sportsgameodds", WARN, f"unreachable ({type(exc).__name__})"))
    okey = os.environ.get("THE_ODDS_API_KEY", "")
    if not okey:
        checks.append(Check("provider_odds_api", FAIL, "THE_ODDS_API_KEY not set (WNBA and fallbacks need it)"))
    else:
        try:
            r = requests.get("https://api.the-odds-api.com/v4/sports", params={"apiKey": okey},
                             timeout=NETWORK_TIMEOUT_S)
            remaining = r.headers.get("x-requests-remaining")
            checks.append(Check("provider_odds_api", PASS if r.status_code == 200 else FAIL,
                                f"HTTP {r.status_code}; credits remaining {remaining}"))
        except Exception as exc:
            checks.append(Check("provider_odds_api", WARN, f"unreachable ({type(exc).__name__})"))
    return checks


# ── runner ──────────────────────────────────────────────────────────


def run_checks(conn, config, skip_network: bool = True) -> list[Check]:
    checks = [
        check_config(config),
        check_database(conn),
        check_schema(conn),
        check_worker_heartbeat(conn),
        check_model_runs(conn),
        check_recommendations_fresh(conn),
        check_odds_data(conn),
        check_auth(conn),
        check_credential_encryption(),
        check_autobet(conn, config),
        check_discord(config),
        check_backups(conn),
    ]
    checks.extend(check_providers(skip_network))
    return checks


def format_report(checks: list[Check]) -> str:
    counts = {s: sum(1 for c in checks if c.status == s) for s in (PASS, WARN, FAIL)}
    lines = [f"PRODUCTION READINESS  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    for c in checks:
        lines.append(f"[{c.status}] {c.subsystem:<24} {c.message}")
    lines += ["", "Note: environment-variable checks (admin password, e-mail, Discord, encryption key, live flags) "
              "reflect THIS process's environment. Run inside each Render service's shell for that service's view.",
              f"PASS={counts[PASS]}  WARN={counts[WARN]}  FAIL={counts[FAIL]}",
              "OVERALL: " + ("NOT READY" if counts[FAIL] else "READY WITH WARNINGS" if counts[WARN] else "READY")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="production_readiness", description=__doc__.split("\n")[0])
    parser.add_argument("--skip-network", action="store_true", help="skip provider connectivity probes")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    from database.db_manager import get_connection
    from src.production_config import load_config

    config = load_config()
    try:
        conn = get_connection()
    except Exception as exc:
        print(f"[FAIL] database                 cannot connect ({type(exc).__name__})")
        return 1
    try:
        checks = run_checks(conn, config, skip_network=args.skip_network)
    finally:
        conn.close()
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2, default=str))
    else:
        print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
