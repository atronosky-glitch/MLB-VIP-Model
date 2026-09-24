"""Per-customer Auto-Bet orchestration, Kalshi + Polymarket (2026-09-23).

MODEL recommendation -> Stage 2A/2B matching+evaluation (REUSED,
completely unmodified: src.execution.paper_cli._gather_qualified_signals,
OpportunityEvaluator.compare) -> for EVERY platform with at least one
qualified opportunity for this signal (VenueComparison.best PLUS
qualified_alternatives -- Stage 2B's cross-venue comparison already
evaluates each enabled provider independently, so a signal that
qualifies on BOTH Kalshi and Polymarket surfaces as two separate
ExecutionOpportunity objects, each with its own .provider) -> for every
customer connected+autobet-enabled on THAT platform -> decrypt THEIR
credentials server-side (src.credential_encryption) -> build a
per-platform provider using ONLY that customer's own key material ->
re-run risk with a context scoped to ONLY that customer's own
exposure/positions on THAT platform (src.execution.live.customer_store)
-> size -> decide PAPER or LIVE -> decide auto-approve (at/under the
customer's own, PER-PLATFORM auto_approve_max_usd cap) vs. queue for
one-tap approval -> record the outcome -- EVERY outcome, including
every skip and failure, never silently -- to customer_autobet_executions,
tagged with which platform.

One official recommendation can therefore execute ONCE on Kalshi AND
ONCE on Polymarket for the same customer, if both are enabled and both
independently qualify -- each platform's claim
(customer_autobet_platform_claims, keyed by (account_id,
recommendation_id, platform)) and duplicate-check
(has_executed_autobet_for_recommendation, platform-scoped) are fully
independent, so neither platform's attempt can block or duplicate the
other's.

PAPER mode is a lightweight, self-contained simulation scoped entirely
within customer_autobet_executions (platform-scoped) -- it reuses
RiskEngine (the pure, config-driven engine, parameterized per customer
per platform) directly, but does NOT use
src.execution.paper.broker.PaperBroker, which is tied to ONE shared
global paper_accounts bankroll; retrofitting that to be multi-tenant
was out of scope for this stage (see the operator-facing report).
Never touches live_* tables, never calls any provider write method.

LIVE mode reuses src.execution.live.approval / service COMPLETELY
UNMODIFIED -- this module never calls the provider's own gated live-
order submission method itself (that would violate
tests/test_live_architecture.py's enforced "only LiveExecutionService
may call this" invariant). A real order is only ever reached via
LiveExecutionService.execute_authorized(), exactly like the operator's
own manual Streamlit flow, for BOTH platforms. The only new concept is
WHO may set an ExecutionAuthorization to APPROVED: a human
(approval.approve(), unchanged, for an order over the customer's own
platform-specific auto_approve_max_usd cap -- queued, never
auto-submitted) or, newly, this module itself calling that SAME
approval.approve() function with approved_by=f"autobet_system:{account_id}",
for an order AT OR UNDER that cap. Every existing global safety gate
(config.live_trading_enabled, config.require_human_approval, the
specific provider's own *_live_enabled, the kill switch, the circuit
breaker) still applies on top, unchanged, re-checked fresh by
execute_authorized() itself -- this module adds a customer-level gate,
it never removes or bypasses an existing one.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import src.customer_kalshi as customer_kalshi
import src.customer_polymarket as customer_polymarket
from database.db_manager import (
    claim_autobet_recommendation,
    get_connection,
    has_executed_autobet_for_recommendation,
    release_autobet_claim,
    save_autobet_execution,
)
from src.credential_encryption import CredentialEncryptionError, decrypt_secret
from src.execution.credentials import CredentialLoadError
from src.execution.evaluator import ExecutionOpportunity, ExecutionSignal
from src.execution.kalshi import KalshiProvider
from src.execution.live import approval as live_approval
from src.execution.live import customer_store as live_customer_store
from src.execution.live import kill_switch
from src.execution.live.models import PreparedLiveOrder, PreparedOrderStatus
from src.execution.live.revalidation import build_live_risk_context
from src.execution.live.service import _can_attempt_live_trade, execute_authorized
from src.execution.live import store as live_store
from src.execution.paper.models import event_identity, fingerprint as compute_fingerprint
from src.execution.polymarket_us import PolymarketUSProvider
from src.execution.risk import RiskContext, RiskEngine

logger = logging.getLogger(__name__)

# Every platform this module knows how to dispatch to. Each has its own
# customer_<platform>_accounts table (see database/db_manager.py) and
# its own customer_<platform>.py module (connect/verify/disconnect/
# risk-settings, sharing the identical shape on purpose -- see
# src/customer_kalshi.py's module docstring for why they're two
# separate tables rather than one generalized one).
PLATFORMS = ("kalshi", "polymarket_us")

_PLATFORM_TABLE = {"kalshi": "customer_kalshi_accounts", "polymarket_us": "customer_polymarket_accounts"}
_PLATFORM_CONNECTED_COLUMN = {"kalshi": "kalshi_connected", "polymarket_us": "polymarket_connected"}
_PLATFORM_MODULE = {"kalshi": customer_kalshi, "polymarket_us": customer_polymarket}

_RISK_CONFIG_OVERRIDES = {
    "unit_size_usd": "unit_size_usd",
    "max_bet_usd": "max_bet_usd",
    "max_daily_loss_usd": "max_daily_loss_usd",
    "max_total_exposure_usd": "max_open_exposure_usd",
    "max_open_positions": "max_open_positions",
}


def _customer_risk_config(base_config: Any, account: dict) -> Any:
    """A copy of the platform config with this customer's OWN risk
    settings overlaid on top -- every field RiskEngine.evaluate() reads
    that the customer hasn't configured (max_trades_per_hour,
    max_event_exposure_usd, etc.) still comes from the platform's own,
    already-reviewed defaults. Never mutates base_config."""
    cfg = copy.copy(base_config)
    for account_field, config_field in _RISK_CONFIG_OVERRIDES.items():
        value = account.get(account_field)
        if value is not None:
            setattr(cfg, config_field, value)
    return cfg


def _all_autobet_enabled_accounts(conn: Any, platform: str) -> list[dict]:
    table = _PLATFORM_TABLE[platform]
    column = _PLATFORM_CONNECTED_COLUMN[platform]
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE {column} = 1 AND autobet_enabled = 1"
    ).fetchall()
    return [dict(r) for r in rows]


def _decrypt_account_credentials(account: dict) -> tuple[str, str] | None:
    """Never logs the decrypted values. Returns None (not an
    exception) on any failure -- a customer's broken/rotated
    credential must never crash the whole Auto-Bet pass for every
    other customer. Column names (encrypted_api_key_id/
    encrypted_private_key) are identical across both platform tables,
    so this needs no platform parameter."""
    encrypted_api_key_id = account.get("encrypted_api_key_id")
    encrypted_private_key = account.get("encrypted_private_key")
    if not encrypted_api_key_id or not encrypted_private_key:
        return None
    try:
        api_key_id = decrypt_secret(encrypted_api_key_id)
        private_key_material = decrypt_secret(encrypted_private_key)
    except CredentialEncryptionError:
        logger.error(
            "Could not decrypt platform credentials for an Auto-Bet account (detail withheld)"
        )
        return None
    return api_key_id, private_key_material


def _build_customer_provider(account: dict, platform: str) -> KalshiProvider | PolymarketUSProvider | None:
    creds = _decrypt_account_credentials(account)
    if creds is None:
        return None
    api_key_id, private_key_material = creds
    try:
        if platform == "kalshi":
            return KalshiProvider(api_key_id=api_key_id, private_key_pem=private_key_material)
        return PolymarketUSProvider(api_key_id=api_key_id, private_key_b64=private_key_material)
    except (CredentialLoadError, ValueError):
        logger.exception("Could not construct a %s provider for an Auto-Bet account", platform)
        return None


def _build_scanning_only_provider(platform: str) -> KalshiProvider | PolymarketUSProvider:
    """A provider for the market-data SCAN phase only
    (get_markets/parse_game_event), deliberately independent of
    whether the OPERATOR has their own global credentials configured
    for this platform (config.kalshi_api_key_id/... or
    config.polymarket_us_api_key_id/...) -- this is a customer-facing
    feature; the operator's own account is a separate, optional
    concern. Uses a throwaway, freshly-generated key that is never used
    for anything authenticated and never leaves this process.

    Confirmed live 2026-09-23 (read-only, zero-credential GET, see the
    session's own verification): Kalshi's GET /markets and
    GET /exchange/status both return real live data with NO auth
    headers sent at all -- market/quote data is genuinely public on
    Kalshi, the same shape as Polymarket US's public gateway. Since
    KalshiProvider._get() always attaches signed headers regardless of
    endpoint (unlike PolymarketUSProvider, which has a real
    public/authenticated host split), a syntactically-valid signature
    from a throwaway key is sent along but not required -- this only
    works because Kalshi's server doesn't reject an unrecognized
    key_id/signature for this specific public endpoint."""
    if platform == "kalshi":
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        throwaway_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = throwaway_key.private_bytes(
            encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return KalshiProvider(api_key_id="scan-only", private_key_pem=pem.decode("ascii"))

    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    throwaway_key = ed25519.Ed25519PrivateKey.generate()
    raw = throwaway_key.private_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return PolymarketUSProvider(api_key_id="scan-only", private_key_b64=base64.b64encode(raw).decode("ascii"))


def _record(conn: Any, **fields: Any) -> None:
    save_autobet_execution(conn, fields)


def _estimate_fee_usd(provider: Any, side: str, price: Decimal, quantity: Decimal) -> float | None:
    """The provider's documented taker-fee estimate for what was actually
    filled, or None if it can't be computed (an unknown fee is recorded
    as unknown -- P&L is then labeled GROSS -- never as zero)."""
    if provider is None or quantity <= 0 or price <= 0:
        return None
    try:
        return float(provider.estimate_fees(side, price, quantity).fee)
    except Exception:
        logger.exception("Could not estimate fees for an Auto-Bet execution")
        return None


def _live_economics(provider: Any, platform: str, side: str | None, live_order: dict | None) -> dict:
    """Execution-economics fields for a LIVE row, from what the venue
    actually reported for the order -- never from the recommendation or
    the intended stake.

    * quantity/avg price: exactly what the order record holds. A
      quantity with no average price is left with fill_source None (the
      fill is unconfirmed), and an average price that is only the order's
      LIMIT price (Kalshi's synchronous response carries no average) is
      tagged ORDER_LIMIT_PRICE -- an upper bound on cost, awaiting
      reconciliation against the venue's fills.
    * fees: the documented estimate re-computed for the FILLED quantity
      (not the requested one), tagged ESTIMATED.
    """
    if not live_order:
        return {}
    filled = Decimal(str(live_order.get("quantity_filled") or 0))
    avg_raw = live_order.get("average_fill_price")
    avg = Decimal(str(avg_raw)) if avg_raw else None
    requested = live_order.get("quantity_requested")
    out: dict = {
        "requested_quantity": float(requested) if requested is not None else None,
        "filled_quantity": float(filled),
        "avg_fill_price": float(avg) if avg is not None else None,
    }
    if filled > 0 and avg is not None:
        out["fill_source"] = "ORDER_LIMIT_PRICE" if platform == "kalshi" else "ORDER_RESPONSE"
        fee = _estimate_fee_usd(provider, side or live_order.get("side") or "", avg, filled)
        if fee is not None:
            out["fees_usd"] = fee
            out["fees_source"] = "ESTIMATED"
    return out


def _paper_risk_context(conn: Any, account_id: str, platform: str, recommendation_id: str) -> RiskContext:
    """PAPER-mode equivalent of build_live_risk_context, computed
    entirely from this customer's OWN customer_autobet_executions
    history for THIS PLATFORM (mode='PAPER', status='EXECUTED') --
    never touches the live_* tables at all, and never mixes a
    customer's Kalshi paper exposure into their Polymarket risk
    context or vice versa (each platform's limits are fully
    independent by design). available_bankroll_usd has no real balance
    to check in paper mode, so it's treated as always sufficient (a
    very large number) -- the customer's own max_bet_usd/
    max_total_exposure_usd caps are what actually bound a paper
    stake."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT * FROM customer_autobet_executions
           WHERE account_id = ? AND platform = ? AND mode = 'PAPER' AND status IN ('EXECUTED', 'PARTIALLY_FILLED')""",
        (account_id, platform),
    ).fetchall()
    rows = [dict(r) for r in rows]
    total_exposure = sum(Decimal(str(r["stake_usd"] or 0)) for r in rows)
    today_rows = [r for r in rows if (r.get("created_at") or "").startswith(today)]
    daily_wagered = sum(Decimal(str(r["stake_usd"] or 0)) for r in today_rows)
    has_duplicate = any(r["recommendation_id"] == recommendation_id for r in rows)
    return RiskContext(
        available_bankroll_usd=Decimal("1000000"),
        open_positions_count=len(rows),
        event_exposure_usd=Decimal("0"),
        provider_exposure_usd=total_exposure,
        sport_exposure_usd=Decimal("0"),
        total_open_exposure_usd=total_exposure,
        daily_wagered_usd=daily_wagered,
        daily_realized_pnl_usd=Decimal("0"),
        trades_in_last_hour=len(today_rows),
        has_open_duplicate=has_duplicate,
        has_settled_duplicate=False,
    )


def _execute_paper(
    conn: Any, config: Any, account: dict, platform: str,
    opportunity: ExecutionOpportunity, signal: ExecutionSignal, provider: Any = None,
) -> str:
    account_id = account["account_id"]
    cfg = _customer_risk_config(config, account)
    unit_size = Decimal(str(account.get("unit_size_usd") or 5.0))
    recommended_stake = min(
        unit_size, Decimal(str(account.get("max_bet_usd") or unit_size)),
    )
    recommended_units = recommended_stake / unit_size if unit_size > 0 else Decimal("0")

    context = _paper_risk_context(conn, account_id, platform, opportunity.recommendation_id)
    decision = RiskEngine(cfg).evaluate(recommended_stake, recommended_units, unit_size, context, False)

    matchup = f"{signal.away_team} @ {signal.home_team}"
    if not decision.approved:
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=opportunity.side,
            model_ev_pct=float(opportunity.net_ev_pct), price_at_detection=float(opportunity.best_ask or 0),
            status="SKIPPED",
            skip_reason=decision.rejection_reason.value if decision.rejection_reason else "RISK_REJECTED",
            mode="PAPER", platform=platform,
        )
        return "SKIPPED"

    # Simulated fill sized from the customer's APPROVED stake (not the
    # analysis stake the opportunity was priced with): whole contracts at
    # the opportunity's expected fill price, cost + estimated fee never
    # above the approved stake. Recorded quantity/price/fees are what P&L
    # is later computed from, so paper results follow the same rules as
    # live ones.
    price = Decimal(str(opportunity.expected_fill_price))
    contracts = (
        (decision.approved_stake_usd / price).to_integral_value(rounding=ROUND_FLOOR)
        if price > 0 else Decimal("0")
    )
    fee = _estimate_fee_usd(provider, opportunity.side, price, contracts)
    while contracts > 0 and contracts * price + Decimal(str(fee or 0)) > decision.approved_stake_usd:
        contracts -= 1
        fee = _estimate_fee_usd(provider, opportunity.side, price, contracts)
    if contracts <= 0:
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=opportunity.side,
            model_ev_pct=float(opportunity.net_ev_pct), price_at_detection=float(opportunity.best_ask or 0),
            stake_usd=float(decision.approved_stake_usd), status="SKIPPED",
            skip_reason="STAKE_BELOW_ONE_CONTRACT", mode="PAPER", platform=platform,
        )
        return "SKIPPED"

    _record(
        conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
        market_type=signal.market_type, matchup=matchup, side=opportunity.side,
        model_ev_pct=float(opportunity.net_ev_pct), price_at_detection=float(opportunity.best_ask or 0),
        price_at_execution=float(price),
        stake_usd=float(decision.approved_stake_usd),
        requested_quantity=float(contracts), filled_quantity=float(contracts), avg_fill_price=float(price),
        fees_usd=fee, fees_source="ESTIMATED" if fee is not None else None, fill_source="SIMULATED",
        status="EXECUTED", mode="PAPER", approval_mode="AUTO", platform=platform,
    )
    return "EXECUTED"


def _execute_live(
    conn: Any, config: Any, account: dict, platform: str,
    opportunity: ExecutionOpportunity, signal: ExecutionSignal, provider: Any,
) -> str:
    account_id = account["account_id"]
    matchup = f"{signal.away_team} @ {signal.home_team}"

    can_trade, gate_detail = _can_attempt_live_trade(config, opportunity.provider)
    if not can_trade:
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=opportunity.side,
            model_ev_pct=float(opportunity.net_ev_pct), status="SKIPPED",
            skip_reason=f"LIVE_TRADING_DISABLED: {gate_detail}", mode="LIVE", platform=platform,
        )
        return "SKIPPED"

    engaged, kill_reason = kill_switch.is_kill_switch_engaged(conn)
    if engaged:
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=opportunity.side,
            status="SKIPPED", skip_reason=f"KILL_SWITCH_ENGAGED: {kill_reason}", mode="LIVE", platform=platform,
        )
        return "SKIPPED"

    cfg = _customer_risk_config(config, account)

    # Reuse approval.prepare_order's OWN sizing (_size) but NOT its
    # internal build_live_risk_context call (global-scoped, wrong for a
    # customer) -- so this module builds the RiskContext itself and
    # calls RiskEngine directly, then persists a PreparedLiveOrder via
    # the same, unmodified live_store.persist_prepared_order.
    side = opportunity.side
    fp = compute_fingerprint(
        opportunity.recommendation_id, opportunity.provider, opportunity.provider_market_id,
        side, signal.line, "live",
    )
    existing = live_store.get_open_prepared_order_by_fingerprint(conn, fp)
    if existing is not None and live_customer_store.get_account_id_for_prepared_order(
        conn, existing["prepared_order_id"]
    ) == account_id:
        return "SKIPPED"  # already queued for this exact customer+platform

    try:
        balance = provider.get_balance()
        available_bankroll = Decimal(str(balance.available))
    except Exception:
        logger.exception("Could not fetch live %s balance for an Auto-Bet account", platform)
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=side, status="FAILED",
            skip_reason="PROVIDER_BALANCE_UNAVAILABLE", mode="LIVE", platform=platform,
        )
        return "FAILED"

    unit_size = Decimal(str(account.get("unit_size_usd") or 5.0))
    recommended_stake = min(unit_size, Decimal(str(account.get("max_bet_usd") or unit_size)), available_bankroll)
    recommended_units = recommended_stake / unit_size if unit_size > 0 else Decimal("0")

    event_id = event_identity(
        signal.league, signal.home_team, signal.away_team,
        signal.event_start_time.date().isoformat() if signal.event_start_time else "unknown",
    )
    context = build_live_risk_context(
        conn, provider, event_id, opportunity.provider, opportunity.league,
        opportunity.recommendation_id, side, account_id=account_id,
    )
    decision = RiskEngine(cfg).evaluate(recommended_stake, recommended_units, unit_size, context, False)
    if not decision.approved:
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=side,
            model_ev_pct=float(opportunity.net_ev_pct), status="SKIPPED",
            skip_reason=decision.rejection_reason.value if decision.rejection_reason else "RISK_REJECTED",
            mode="LIVE", platform=platform,
        )
        return "SKIPPED"

    now = datetime.now(timezone.utc)
    order = PreparedLiveOrder(
        prepared_order_id=None, opportunity_id=None, recommendation_id=opportunity.recommendation_id,
        provider=opportunity.provider, provider_market_id=opportunity.provider_market_id,
        league=opportunity.league, event=opportunity.event, event_id=event_id, side=side,
        event_start_time=signal.event_start_time, model_probability=opportunity.model_probability,
        current_price=opportunity.best_ask or opportunity.expected_fill_price,
        expected_fill_price=opportunity.expected_fill_price, net_ev_pct=opportunity.net_ev_pct,
        recommended_units=recommended_units, recommended_stake=recommended_stake,
        risk_approved_stake=decision.approved_stake_usd, risk_approved_units=decision.approved_units,
        quantity=opportunity.quantity_analyzed, maximum_entry_price=opportunity.max_acceptable_price,
        fees_estimate=opportunity.estimated_fees, slippage_estimate=opportunity.expected_slippage,
        available_liquidity=opportunity.available_liquidity, fingerprint=fp,
        risk_snapshot={
            "limiting_constraint": decision.limiting_constraint.value if decision.limiting_constraint else None,
            "checks": list(decision.checks), "available_bankroll": str(available_bankroll),
        },
        status=PreparedOrderStatus.READY, created_at=now,
        expires_at=now + timedelta(seconds=int(config.max_opportunity_age_seconds)),
    )
    prepared_order_id = live_store.persist_prepared_order(conn, order)
    live_customer_store.tag_prepared_order_with_account(conn, prepared_order_id, account_id)
    live_store.log_event(
        conn, "PREPARED", f"auto-bet account={account_id} platform={platform} net_ev_pct={opportunity.net_ev_pct}",
        prepared_order_id=prepared_order_id,
    )

    # The customer's own auto_approve_max_usd is necessary but never
    # sufficient for unattended execution -- it must ALSO be at/under the
    # operator-controlled server-side hard cap (config.<platform>_autobet_
    # server_max_order_usd), which no customer setting can override. This
    # is a genuine additional ceiling, not a re-statement of the customer
    # cap: an order can be under a generous customer cap yet still exceed
    # the server-wide safety limit.
    auto_approve_cap = account.get("auto_approve_max_usd")
    server_cap = getattr(config, f"{platform}_autobet_server_max_order_usd", None)
    should_auto_approve = (
        auto_approve_cap is not None
        and server_cap is not None
        and decision.approved_stake_usd <= Decimal(str(auto_approve_cap))
        and decision.approved_stake_usd <= Decimal(str(server_cap))
    )

    if not should_auto_approve:
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=side,
            model_ev_pct=float(opportunity.net_ev_pct), price_at_detection=float(opportunity.best_ask or 0),
            stake_usd=float(decision.approved_stake_usd), status="SKIPPED",
            skip_reason="AWAITING_MANUAL_APPROVAL", mode="LIVE", approval_mode="MANUAL", platform=platform,
        )
        return "SKIPPED"  # queued in prepared_live_orders; claim stays held so it isn't re-queued

    authorization = live_approval.approve(conn, prepared_order_id, config, approved_by=f"autobet_system:{account_id}")
    if authorization is None:
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=side, status="FAILED",
            skip_reason="AUTO_APPROVAL_FAILED", mode="LIVE", platform=platform,
        )
        return "FAILED"

    live_customer_store.tag_authorization_with_account(conn, authorization.approval_id, account_id, "AUTO")

    result = execute_authorized(conn, authorization.approval_id, provider, config)

    if result.outcome == "SUBMITTED" and result.live_order_id is not None:
        live_order = live_store.get_live_order(conn, result.live_order_id)
        if live_order:
            position = conn.execute(
                "SELECT position_id FROM live_positions WHERE live_order_id = ?", (result.live_order_id,)
            ).fetchone()
            if position:
                live_customer_store.tag_position_with_account(conn, dict(position)["position_id"], account_id)
        status = "EXECUTED" if (not live_order or live_order.get("status") != "PARTIALLY_FILLED") else "PARTIALLY_FILLED"
        _record(
            conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
            market_type=signal.market_type, matchup=matchup, side=side,
            model_ev_pct=float(opportunity.net_ev_pct), price_at_detection=float(opportunity.best_ask or 0),
            price_at_execution=float(live_order.get("average_fill_price") or opportunity.expected_fill_price) if live_order else None,
            stake_usd=float(decision.approved_stake_usd),
            provider_order_id=live_order.get("provider_order_id") if live_order else None,
            status=status, mode="LIVE", approval_mode="AUTO", platform=platform,
            **_live_economics(provider, platform, side, live_order),
        )
        return status

    _record(
        conn, account_id=account_id, recommendation_id=opportunity.recommendation_id,
        market_type=signal.market_type, matchup=matchup, side=side,
        model_ev_pct=float(opportunity.net_ev_pct), status="FAILED",
        skip_reason=f"{result.reason}: {result.detail}", mode="LIVE", approval_mode="AUTO", platform=platform,
    )
    return "FAILED"


def approve_pending_order(
    conn: Any, config: Any, account_id: str, platform: str, prepared_order_id: int,
) -> tuple[bool, str]:
    """Customer-initiated approval for an over-cap LIVE order this
    module queued for manual approval (_execute_live's
    AWAITING_MANUAL_APPROVAL path -- see the customer-facing "pending
    approval" queue in src/customer_view.py). Mirrors _execute_live's
    own auto-approve tail exactly; only approved_by/approval_mode
    differ, reflecting a real human click instead of the automatic
    under-cap check. Every existing safety gate (config gates, kill
    switch, revalidation) still applies unchanged inside
    execute_authorized() -- this function does not bypass any of them."""
    prepared = live_store.get_prepared_order(conn, prepared_order_id)
    if prepared is None or prepared["status"] != "READY":
        return False, "This order is no longer awaiting approval."
    if live_customer_store.get_account_id_for_prepared_order(conn, prepared_order_id) != account_id:
        return False, "This order does not belong to your account."
    if prepared.get("provider") != platform:
        return False, "This order does not belong to that platform."

    table = _PLATFORM_TABLE[platform]
    account_row = conn.execute(f"SELECT * FROM {table} WHERE account_id = ?", (account_id,)).fetchone()
    if account_row is None:
        return False, "Account not found."
    account = dict(account_row)

    provider = _build_customer_provider(account, platform)
    if provider is None:
        return False, "Could not use your saved credentials -- please reconnect your account."

    authorization = live_approval.approve(conn, prepared_order_id, config, approved_by=f"customer:{account_id}")
    if authorization is None:
        return False, "Could not approve this order -- it may have expired or already been handled."
    live_customer_store.tag_authorization_with_account(conn, authorization.approval_id, account_id, "MANUAL")

    result = execute_authorized(conn, authorization.approval_id, provider, config)
    matchup = prepared.get("event") or ""

    if result.outcome == "SUBMITTED" and result.live_order_id is not None:
        live_order = live_store.get_live_order(conn, result.live_order_id)
        if live_order:
            position = conn.execute(
                "SELECT position_id FROM live_positions WHERE live_order_id = ?", (result.live_order_id,)
            ).fetchone()
            if position:
                live_customer_store.tag_position_with_account(conn, dict(position)["position_id"], account_id)
        status = "EXECUTED" if (not live_order or live_order.get("status") != "PARTIALLY_FILLED") else "PARTIALLY_FILLED"
        _record(
            conn, account_id=account_id, recommendation_id=prepared["recommendation_id"],
            matchup=matchup, side=prepared.get("side"),
            price_at_execution=float(live_order.get("average_fill_price") or 0) if live_order else None,
            stake_usd=float(prepared.get("risk_approved_stake") or 0),
            provider_order_id=live_order.get("provider_order_id") if live_order else None,
            status=status, mode="LIVE", approval_mode="MANUAL", platform=platform,
            **_live_economics(provider, platform, prepared.get("side"), live_order),
        )
        return True, f"Order {status.lower()}."

    _record(
        conn, account_id=account_id, recommendation_id=prepared["recommendation_id"],
        matchup=matchup, side=prepared.get("side"), status="FAILED",
        skip_reason=f"{result.reason}: {result.detail}", mode="LIVE", approval_mode="MANUAL", platform=platform,
    )
    return False, f"Order could not be executed: {result.detail}"


def reject_pending_order(
    conn: Any, account_id: str, platform: str, prepared_order_id: int, reason: str | None = None,
) -> bool:
    """The customer's explicit "no" on a queued over-cap order. Releases
    the recommendation's claim (mirroring every other non-terminal
    outcome in process_recommendation_for_account) so a later scan may
    reconsider it, e.g. at a smaller size or once conditions change."""
    prepared = live_store.get_prepared_order(conn, prepared_order_id)
    if prepared is None:
        return False
    if live_customer_store.get_account_id_for_prepared_order(conn, prepared_order_id) != account_id:
        return False
    if prepared.get("provider") != platform:
        return False
    ok = live_approval.reject(conn, prepared_order_id, reason)
    if not ok:
        return False
    _record(
        conn, account_id=account_id, recommendation_id=prepared["recommendation_id"],
        matchup=prepared.get("event"), side=prepared.get("side"), status="SKIPPED",
        skip_reason="MANUALLY_REJECTED", mode="LIVE", approval_mode="MANUAL", platform=platform,
    )
    release_autobet_claim(conn, account_id, prepared["recommendation_id"], platform)
    return True


def process_recommendation_for_account(
    conn: Any, config: Any, account: dict, platform: str,
    opportunity: ExecutionOpportunity, signal: ExecutionSignal,
) -> str:
    """Attempt to execute ONE qualified Stage 2B opportunity, on ONE
    platform, for ONE customer. Returns the resulting status. Never
    raises -- every failure mode is caught and recorded as a
    FAILED/SKIPPED row, never silently dropped. *opportunity* must
    already be the platform-matching ExecutionOpportunity (i.e.
    opportunity.provider == platform) -- see run_customer_autobet_pass,
    which resolves that from VenueComparison before calling this."""
    account_id = account["account_id"]
    recommendation_id = signal.recommendation_id

    if has_executed_autobet_for_recommendation(conn, account_id, recommendation_id, platform):
        return "SKIPPED"

    if opportunity is None or opportunity.provider != platform:
        return "SKIPPED"

    sport_filter = (account.get("sport_filter") or "").strip()
    if sport_filter:
        allowed = {s.strip().upper() for s in sport_filter.split(",") if s.strip()}
        if signal.league.upper() not in allowed:
            return "SKIPPED"

    min_ev = account.get("min_net_ev_pct")
    if min_ev is not None and float(opportunity.net_ev_pct) < float(min_ev):
        return "SKIPPED"

    if not claim_autobet_recommendation(conn, account_id, recommendation_id, platform):
        return "SKIPPED"

    try:
        provider = _build_customer_provider(account, platform)
        if provider is None:
            _record(
                conn, account_id=account_id, recommendation_id=recommendation_id, status="FAILED",
                skip_reason="CREDENTIALS_UNAVAILABLE", mode="LIVE" if account.get("live_execution") else "PAPER",
                platform=platform,
            )
            release_autobet_claim(conn, account_id, recommendation_id, platform)
            return "FAILED"

        if account.get("live_execution"):
            status = _execute_live(conn, config, account, platform, opportunity, signal, provider)
        else:
            status = _execute_paper(conn, config, account, platform, opportunity, signal, provider)

        if status not in ("EXECUTED", "PARTIALLY_FILLED"):
            # AWAITING_MANUAL_APPROVAL deliberately keeps its claim
            # (see _execute_live) -- everything else is free to retry.
            already_pending = status == "SKIPPED" and conn.execute(
                "SELECT 1 FROM customer_autobet_executions WHERE account_id = ? AND recommendation_id = ? "
                "AND platform = ? AND skip_reason = 'AWAITING_MANUAL_APPROVAL' ORDER BY created_at DESC LIMIT 1",
                (account_id, recommendation_id, platform),
            ).fetchone()
            if not already_pending:
                release_autobet_claim(conn, account_id, recommendation_id, platform)
        return status
    except Exception:
        logger.exception("Unexpected error processing Auto-Bet for a customer account")
        _record(
            conn, account_id=account_id, recommendation_id=recommendation_id, status="FAILED",
            skip_reason="UNEXPECTED_ERROR", mode="LIVE" if account.get("live_execution") else "PAPER",
            platform=platform,
        )
        release_autobet_claim(conn, account_id, recommendation_id, platform)
        return "FAILED"


def _scan_and_dispatch(config: Any) -> dict:
    """The top-level entry point src/worker.py calls. Gathers today's
    qualified Stage 2B opportunities ONCE (reused across every customer
    and both platforms, not re-fetched per customer), then attempts
    execution for every connected + Auto-Bet-enabled customer on every
    platform they've independently enabled. One platform's provider
    failure (e.g. Kalshi's public market feed down) never blocks the
    other platform's pass -- each is gathered and dispatched
    independently."""
    from src.execution.cli import _load_actionable_rows
    from src.execution.paper_cli import _gather_qualified_signals

    conn = get_connection(config.database_path)
    try:
        accounts_by_platform = {p: _all_autobet_enabled_accounts(conn, p) for p in PLATFORMS}
        total_accounts = sum(len(v) for v in accounts_by_platform.values())
        if total_accounts == 0:
            return {"accounts_considered": 0, "executed": 0, "skipped": 0, "failed": 0}

        providers: dict[str, Any] = {}
        for platform in PLATFORMS:
            if not accounts_by_platform[platform]:
                continue
            try:
                providers[platform] = _build_scanning_only_provider(platform)
            except Exception:
                logger.exception("Could not initialize the read-only %s provider for scanning", platform)

        if not providers:
            return {
                "accounts_considered": total_accounts, "executed": 0, "skipped": 0, "failed": 0,
                "error": "PROVIDER_INIT_FAILED",
            }

        rows = _load_actionable_rows(config)
        gathered = _gather_qualified_signals(config, providers, rows, None, None)

        counts = {"executed": 0, "skipped": 0, "failed": 0}
        for kind, rec_event, signal, comparison in gathered:
            if kind != "evaluated" or comparison is None:
                continue
            # Every provider that independently qualified for this
            # signal -- not just the single cross-venue "best" -- so a
            # customer with BOTH platforms enabled can execute on each
            # one independently for the SAME recommendation.
            candidate_opportunities = list(comparison.qualified_alternatives)
            if comparison.best is not None:
                candidate_opportunities.append(comparison.best)
            for opportunity in candidate_opportunities:
                platform = opportunity.provider
                accounts = accounts_by_platform.get(platform)
                if not accounts:
                    continue
                for account in accounts:
                    status = process_recommendation_for_account(conn, config, account, platform, opportunity, signal)
                    if status in ("EXECUTED", "PARTIALLY_FILLED"):
                        counts["executed"] += 1
                    elif status == "FAILED":
                        counts["failed"] += 1
                    else:
                        counts["skipped"] += 1

        return {"accounts_considered": total_accounts, **counts}
    finally:
        conn.close()



def run_customer_autobet_pass(config: Any) -> dict:
    """Worker entry point: dispatch new qualified recommendations, then
    reconcile LIVE Kalshi executions against the venue's own fills
    (read-only, src/execution/autobet_reconcile.py). The two are
    independent: a reconciliation problem never fails or hides the
    dispatch result, and unreconciled rows stay labeled as estimates in
    My Performance until reconciliation succeeds."""
    result = _scan_and_dispatch(config)
    from src.execution.autobet_reconcile import reconcile_customer_fills

    conn = get_connection(config.database_path)
    try:
        result["reconciliation"] = reconcile_customer_fills(conn)
    except Exception:
        logger.exception("Customer fill reconciliation failed")
        result["reconciliation"] = {"error": True}
    finally:
        conn.close()
    return result
