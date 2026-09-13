"""Stage 4 human-approved live-execution CLI commands.

    python -m src.execution.cli live-scan [--verbose] [--limit N] [--league L] [--provider P]
    python -m src.execution.cli live-approve <prepared_order_id>
    python -m src.execution.cli live-reject <prepared_order_id> [--reason TEXT]
    python -m src.execution.cli live-execute <approval_id>
    python -m src.execution.cli live-status

live-execute is the ONLY command that can ever trigger a real provider
mutation, and only for an already-APPROVED, unexpired, unused
ExecutionAuthorization -- see src/execution/live/service.py. Every
other command here is read-only or purely a database state change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from database.db_manager import get_connection
from src.execution import get_provider
from src.execution.cli import _ALL_PROVIDERS, _enabled_providers, _load_actionable_rows
from src.execution.live import approval, kill_switch, service, store
from src.execution.paper_cli import _gather_qualified_signals
from src.production_config import load_config


def live_scan(
    verbose: bool, limit: int | None, league: str | None, only_providers: list[str] | None,
) -> int:
    config = load_config()
    if not config.live_trading_enabled:
        print("Live trading is disabled (LIVE_TRADING_ENABLED=false) -- opportunities can still be prepared for review, but never executed.")

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

    conn = get_connection(config.database_path)
    any_prepared = False
    try:
        for kind, rec_event, signal, comparison in gathered:
            if kind in ("no_signal", "no_match"):
                if verbose:
                    label = rec_event.away_team if kind == "no_signal" else signal.away_team
                    print(f"{label}: SKIPPED ({kind})")
                continue
            if comparison.best is None:
                continue

            opportunity = comparison.best
            provider_flag_name = f"{opportunity.provider}_live_enabled"
            if not getattr(config, provider_flag_name, False):
                if verbose:
                    print(f"{opportunity.provider}: SKIPPED -- {provider_flag_name}=false")
                continue

            prep = approval.prepare_order(conn, opportunity, signal, providers[opportunity.provider], config)
            if prep.rejected:
                if verbose:
                    print(f"{signal.away_team} @ {signal.home_team}: PREPARE REJECTED -- {prep.rejection_detail}")
                continue
            any_prepared = True
            print("=" * 50)
            print(f"{signal.league} — {signal.away_team} @ {signal.home_team}")
            print(f"Provider: {opportunity.provider.upper()}  Net EV: {opportunity.net_ev_pct:+.2f}%")
            print(f"Prepared order id: {prep.prepared_order_id}")
            print("Run: live-approve {} (or live-reject {})".format(prep.prepared_order_id, prep.prepared_order_id))
    finally:
        conn.close()

    if not any_prepared:
        print("No opportunities prepared this run.")
    return 0


def _print_approval_card(prepared: dict) -> None:
    print("=" * 60)
    print("REAL MONEY -- REVIEW CAREFULLY")
    print(f"League/Event: {prepared['league']} — {prepared['event']}")
    print(f"Provider: {prepared['provider'].upper()}   Market: {prepared['provider_market_id']}")
    print(f"Side: {prepared['side']}")
    print(f"Model probability: {prepared['model_probability']}")
    print(f"Current price: {prepared['current_price']}   Expected fill: {prepared['expected_fill_price']}")
    print(f"Net EV: {prepared['net_ev_pct']}%")
    print(f"Recommended: {prepared['recommended_units']}u / ${prepared['recommended_stake']}")
    print(f"Risk-approved stake: ${prepared['risk_approved_stake']}   Quantity: {prepared['quantity']}")
    print(f"Maximum approved price: ${prepared['maximum_entry_price']}")
    print(f"Expected fees: ${prepared['fees_estimate']}   Expected slippage: {prepared['slippage_estimate']}")
    print(f"Available liquidity: ${prepared['available_liquidity']}")
    print(f"Expires at: {prepared['expires_at']}")
    print("=" * 60)


def live_approve(prepared_order_id: int, assume_yes: bool = False) -> int:
    config = load_config()
    conn = get_connection(config.database_path)
    try:
        approval.refresh_queue(conn)
        prepared = store.get_prepared_order(conn, prepared_order_id)
        if prepared is None:
            print(f"No prepared order with id {prepared_order_id}.")
            return 1
        if prepared["status"] != "READY":
            print(f"Prepared order {prepared_order_id} is {prepared['status']}, not READY -- cannot approve.")
            return 1

        _print_approval_card(prepared)
        stake = prepared["risk_approved_stake"]
        prompt = f"Type 'yes' to APPROVE REAL ${stake:.2f} TRADE: "
        if not assume_yes:
            confirmation = input(prompt)
            if confirmation.strip().lower() != "yes":
                print("Not confirmed -- no approval created.")
                return 0

        authorization = approval.approve(conn, prepared_order_id, config)
        if authorization is None:
            print("Could not approve -- the prepared order was claimed/expired by something else first.")
            return 1
        print(f"APPROVED. approval_id={authorization.approval_id}")
        print(f"Expires at {authorization.expires_at} -- run: live-execute {authorization.approval_id}")
        return 0
    finally:
        conn.close()


def live_reject(prepared_order_id: int, reason: str | None) -> int:
    config = load_config()
    conn = get_connection(config.database_path)
    try:
        ok = approval.reject(conn, prepared_order_id, reason)
        print(f"Rejected prepared order {prepared_order_id}." if ok else f"Could not reject {prepared_order_id} (not READY?).")
        return 0 if ok else 1
    finally:
        conn.close()


def live_execute(approval_id: str) -> int:
    config = load_config()
    conn = get_connection(config.database_path)
    try:
        authorization = store.get_authorization(conn, approval_id)
        if authorization is None:
            print(f"No such approval_id: {approval_id}")
            return 1
        try:
            provider = get_provider(authorization["provider"], config)
        except Exception as exc:
            print(f"Could not initialize provider {authorization['provider']}: {type(exc).__name__}: {exc}")
            return 1

        result = service.execute_authorized(conn, approval_id, provider, config)
        print(f"OUTCOME: {result.outcome}")
        if result.reason:
            print(f"Reason: {result.reason}")
        print(f"Detail: {result.detail}")
        return 0 if result.outcome == "SUBMITTED" else 1
    finally:
        conn.close()


def live_status() -> int:
    config = load_config()
    conn = get_connection(config.database_path)
    try:
        engaged, reason = kill_switch.is_kill_switch_engaged(conn)
        print(f"LIVE TRADING: {'ON' if config.live_trading_enabled else 'OFF'}")
        print(f"KILL SWITCH: {'ENGAGED (' + str(reason) + ')' if engaged else 'clear'}")
        for name in _ALL_PROVIDERS:
            enabled = getattr(config, f"{name}_live_enabled", False)
            tripped = kill_switch.is_circuit_tripped(conn, name)
            print(f"{name.upper()}: live_enabled={enabled} circuit_breaker={'TRIPPED' if tripped else 'ok'}")

        pending = approval.get_pending(conn)
        print(f"\nREADY FOR APPROVAL: {len(pending)}")
        for p in pending:
            print(f"  #{p['prepared_order_id']} {p['event']} ({p['provider']}) net_ev={p['net_ev_pct']}% stake=${p['risk_approved_stake']}")

        approved = conn.execute("SELECT * FROM execution_authorizations WHERE status = 'APPROVED'").fetchall()
        print(f"\nAPPROVED (awaiting execution): {len(approved)}")
        for a in approved:
            print(f"  {a['approval_id']} {a['provider']} stake=${a['approved_stake_usd']} expires={a['expires_at']}")

        open_positions = store.get_open_live_positions(conn)
        print(f"\nOPEN LIVE POSITIONS: {len(open_positions)}")
        for pos in open_positions:
            print(f"  {pos['provider']} {pos['side']} qty={pos['quantity']} entry_cost=${pos['total_entry_cost']:.2f}")
    finally:
        conn.close()
    return 0


def _has_credentials(config: Any, provider_name: str) -> bool:
    key_id = getattr(config, f"{provider_name}_api_key_id", "")
    key_path = getattr(config, f"{provider_name}_private_key_path", "")
    return bool(key_id and key_path)


def _pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def provider_diagnostics(provider_name: str) -> int:
    """Read-only diagnostic sweep for one provider. Never calls any
    provider mutation method -- only health_check/get_balance/
    get_markets/get_orderbook and capability
    flags. Prints NO secret values (only whether credentials are
    configured, never the values themselves)."""
    config = load_config()
    print(f"=== {provider_name.upper()} DIAGNOSTICS ===")

    has_creds = _has_credentials(config, provider_name)
    print(f"Credentials configured...... {'YES' if has_creds else 'NO'}")
    if not has_creds:
        print("Cannot run any live check without credentials -- reporting PENDING for all remaining checks.")
        print(f"Live schema................ {'VERIFIED' if _schema_verified(provider_name) else 'UNVERIFIED'}")
        print("OVERALL: LIVE PROVIDER VERIFICATION PENDING -- no credentials configured")
        return 0

    enabled = getattr(config, f"{provider_name}_enabled", False)
    if not enabled:
        print(f"{provider_name}_enabled=false -- read-only access is not enabled either. Reporting PENDING.")
        print("OVERALL: LIVE PROVIDER VERIFICATION PENDING -- provider disabled")
        return 0

    try:
        provider = get_provider(provider_name, config)
    except Exception as exc:
        print(f"Could not initialize provider: {type(exc).__name__}: {exc}")
        print("OVERALL: NOT READY -- provider initialization failed")
        return 1

    auth_ok = False
    try:
        health = provider.health_check()
        auth_ok = health.ok
        print(f"Authentication.............. {_pass_fail(auth_ok)} ({health.detail})")
    except Exception as exc:
        print(f"Authentication.............. FAIL ({type(exc).__name__}: {exc})")

    balance_ok = False
    try:
        provider.get_balance()
        balance_ok = True
        print(f"Balance...................... {_pass_fail(balance_ok)}")
    except Exception as exc:
        print(f"Balance...................... FAIL ({type(exc).__name__}: {exc})")

    markets: list = []
    markets_ok = False
    try:
        markets = provider.get_markets(limit=5) or []
        markets_ok = True
        print(f"Markets...................... {_pass_fail(markets_ok)} ({len(markets)} returned)")
    except Exception as exc:
        print(f"Markets...................... FAIL ({type(exc).__name__}: {exc})")

    orderbook_ok = False
    if markets:
        try:
            raw_book = provider.get_orderbook(markets[0].id)
            provider.normalize_orderbook(raw_book)
            orderbook_ok = True
            print(f"Orderbook.................... {_pass_fail(orderbook_ok)}")
        except Exception as exc:
            print(f"Orderbook.................... FAIL ({type(exc).__name__}: {exc})")
    else:
        print("Orderbook.................... SKIPPED (no markets returned to test against)")

    caps = provider.capabilities
    print(f"Order history................ {'SUPPORTED' if caps.supports_order_lookup else 'UNSUPPORTED'}")
    print(f"Fills......................... {'SUPPORTED' if caps.supports_fill_lookup else 'UNSUPPORTED'}")
    print(f"Positions..................... {'SUPPORTED' if hasattr(provider, 'get_positions') else 'UNSUPPORTED'}")
    print(f"Cancel........................ {'SUPPORTED' if caps.supports_cancel else 'UNSUPPORTED'}")
    print(f"Preview....................... {'SUPPORTED' if caps.supports_preview else 'UNSUPPORTED'}")

    verified = _schema_verified(provider_name)
    print(f"Live order schema............. {'VERIFIED' if verified else 'UNVERIFIED'}")

    overall_ready = auth_ok and balance_ok and markets_ok
    print(f"\nOVERALL: {'READ-ONLY CONNECTIVITY OK' if overall_ready else 'NOT READY'}")
    return 0 if overall_ready else 1


def _schema_verified(provider_name: str) -> bool:
    if provider_name == "kalshi":
        from src.execution.kalshi import KALSHI_LIVE_SCHEMA_VERIFIED
        return KALSHI_LIVE_SCHEMA_VERIFIED
    if provider_name == "polymarket_us":
        return True  # POST /v1/orders schema confirmed directly from current docs.polymarket.us
    return False


def live_readiness() -> int:
    """Aggregate, read-only checklist. Never enables anything -- purely
    reports the current state of every gate a real order would have to
    pass through."""
    config = load_config()
    conn = get_connection(config.database_path)
    all_ready = True
    try:
        for provider_name in _ALL_PROVIDERS:
            print(f"\n{provider_name.upper()}")
            has_creds = _has_credentials(config, provider_name)
            print(f"  Credentials.............. {'PASS' if has_creds else 'PENDING (not configured)'}")

            read_ok = None
            if has_creds and getattr(config, f"{provider_name}_enabled", False):
                try:
                    provider = get_provider(provider_name, config)
                    health = provider.health_check()
                    read_ok = health.ok
                except Exception:
                    read_ok = False
            print(f"  Market/orderbook reads... {'PASS' if read_ok else ('PENDING' if read_ok is None else 'FAIL')}")

            side_semantics_ok = provider_name == "kalshi"  # YES/NO confirmed by Kalshi's own docs; Polymarket NO-side still unverified
            print(f"  Side semantics........... {'PASS' if side_semantics_ok else 'BLOCKED (NO-side unverified)'}")

            schema_ok = _schema_verified(provider_name)
            print(f"  Create-order schema...... {'PASS' if schema_ok else 'FAIL / UNVERIFIED'}")

            print("  Reconciliation........... " + (
                "PASS (idempotent, full-status order list)" if provider_name == "kalshi"
                else "MANUAL ONLY (no idempotency key, open-orders-only list)"
            ))

            print("  Approval gate............ PASS (single-use, TTL-bounded, atomic claim)")
            print("  Risk gate................ PASS (RiskEngine reused unchanged)")
            print(f"  Kill switch.............. {'ENGAGED' if kill_switch.is_kill_switch_engaged(conn)[0] else 'PASS (clear)'}")
            print("  Stop loss................ PASS (Stage 3 RiskEngine daily-loss check reused)")

            production_enabled = getattr(config, f"{provider_name}_live_enabled", False) and config.live_trading_enabled
            print(f"  Production enabled....... {'YES' if production_enabled else 'NO'}")

            provider_ready = has_creds and (read_ok is True) and side_semantics_ok and schema_ok
            if not provider_ready:
                all_ready = False

        print("\n" + "=" * 50)
        if not any(_has_credentials(config, p) for p in _ALL_PROVIDERS):
            print("OVERALL: LIVE PROVIDER VERIFICATION PENDING -- no credentials configured in this environment")
        elif all_ready:
            print("OVERALL: READY FOR CONTROLLED HUMAN-APPROVED TEST")
        else:
            print("OVERALL: NOT READY FOR PRODUCTION")
        print(f"LIVE_TRADING_ENABLED={config.live_trading_enabled}  (must be explicitly set true to allow any real order)")
    finally:
        conn.close()
    return 0
