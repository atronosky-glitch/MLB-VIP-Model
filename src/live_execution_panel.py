"""Streamlit "Live Execution" tab -- Stage 4's human-approval surface.

IMPORTANT: Streamlit is the DISPLAY surface, not the security boundary.
Every enforcement check (LIVE_TRADING_ENABLED, REQUIRE_HUMAN_APPROVAL,
per-provider live flags, the kill switch, RiskEngine, fresh revalidation)
happens in src/execution/live/service.py and runs identically whether
this button, a CLI command, or anything else calls into it. This module
never builds a provider request, never calls a provider mutation method,
and never lets a user type/edit trade parameters -- every number shown
comes from a server-side PreparedLiveOrder.

Also important: st.session_state is used ONLY for this page's own
"LIVE MODE" convenience toggle (an additional friction-reducer that
resets to OFF on every fresh process, per the Stage 4 plan) -- it is
NEVER used as the actual authorization record. The real,
single-use, persisted authorization is the ExecutionAuthorization row
in the database, created by clicking Approve.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from database.db_manager import get_connection
from src.execution import get_provider
from src.execution.live import approval, kill_switch, service, store


def render_live_execution_tab(config: Any, db_path: str) -> None:
    st.subheader(":material/bolt: Live Execution")
    st.warning(
        "**Local-only human-approval surface.** This tab is not safe for public/remote exposure -- "
        "there is no authentication layer. Every approval and execution is still enforced server-side "
        "(RiskEngine, fresh market revalidation, a single-use authorization) regardless of this UI.",
        icon=":material/warning:",
    )

    st.session_state.setdefault("live_mode_on", False)

    top_cols = st.columns(4)
    with top_cols[0]:
        st.metric("LIVE TRADING (config)", "ON" if config.live_trading_enabled else "OFF")
    with top_cols[1]:
        st.session_state.live_mode_on = st.toggle(
            "LIVE MODE (this session)", value=st.session_state.live_mode_on,
            help="Resets to OFF every time this app restarts. Both this AND LIVE_TRADING_ENABLED "
                 "must be true for an approval to be executable -- this toggle alone changes nothing server-side.",
        )
    with top_cols[2]:
        st.metric("MODE", "LIVE" if config.live_trading_enabled and st.session_state.live_mode_on else "PAPER-ONLY")

    conn = get_connection(db_path)
    try:
        engaged, kill_reason = kill_switch.is_kill_switch_engaged(conn)
        with top_cols[3]:
            st.metric("KILL SWITCH", "ENGAGED" if engaged else "clear")

        kill_cols = st.columns(2)
        with kill_cols[0]:
            if not engaged and st.button(":material/emergency_home: ENGAGE KILL SWITCH", type="secondary"):
                kill_switch.engage_kill_switch(conn, "engaged from Streamlit control panel")
                st.rerun()
        with kill_cols[1]:
            if engaged and st.button(":material/replay: Disengage kill switch"):
                kill_switch.disengage_kill_switch(conn)
                st.rerun()
        if engaged:
            st.error(f"Kill switch engaged: {kill_reason}. No new real orders will be submitted; open positions keep tracking.")

        st.divider()
        st.markdown("**Providers**")
        provider_cols = st.columns(2)
        for col, name in zip(provider_cols, ("kalshi", "polymarket_us")):
            with col:
                enabled = getattr(config, f"{name}_enabled", False)
                live_enabled = getattr(config, f"{name}_live_enabled", False)
                tripped = kill_switch.is_circuit_tripped(conn, name)
                st.markdown(f"**{name.upper()}**")
                st.caption(f"Read-only enabled: {enabled} · Live enabled: {live_enabled}")
                if tripped:
                    st.error("PROVIDER LIVE EXECUTION PAUSED — ERROR CIRCUIT BREAKER")
                if enabled:
                    try:
                        provider = get_provider(name, config)
                        health = provider.health_check()
                        st.caption(f"Connectivity: {'OK' if health.ok else 'FAIL'} ({health.detail})")
                        if live_enabled:
                            balance = provider.get_balance()
                            st.caption(f"Balance: {balance.available} {balance.currency}")
                    except Exception as exc:
                        st.caption(f"Connectivity check failed: {type(exc).__name__}: {exc}")
                else:
                    st.caption("Disabled")

        st.divider()
        _render_ready_for_approval(conn, config)
        st.divider()
        _render_approved_awaiting_execution(conn, config)
        st.divider()
        _render_recent_activity(conn)
    finally:
        conn.close()


def _render_ready_for_approval(conn: Any, config: Any) -> None:
    st.markdown("### READY FOR APPROVAL")
    pending = approval.get_pending(conn)  # already sorted by net EV desc; expires stale cards to EXPIRED
    if not pending:
        st.caption("No prepared orders awaiting approval. Run `live-scan` to prepare some.")
        return

    for prepared in pending:
        with st.container(border=True):
            cols = st.columns([3, 1])
            with cols[0]:
                st.markdown(f"**{prepared['league']} — {prepared['event']}**")
                st.caption(
                    f"{prepared['provider'].upper()} · side={prepared['side']} · "
                    f"net EV={prepared['net_ev_pct']:.2f}% · expires {prepared['expires_at']}"
                )
                detail_cols = st.columns(4)
                detail_cols[0].metric("Model prob.", f"{prepared['model_probability']:.2f}")
                detail_cols[1].metric("Expected fill", f"${prepared['expected_fill_price']:.2f}")
                detail_cols[2].metric("Max price", f"${prepared['maximum_entry_price']:.2f}")
                detail_cols[3].metric("Quantity", f"{prepared['quantity']:.0f}")
                st.caption(
                    f"Risk-approved stake: ${prepared['risk_approved_stake']:.2f} "
                    f"({prepared['risk_approved_units']}u) · fees≈${prepared['fees_estimate']:.2f} · "
                    f"liquidity=${prepared['available_liquidity']:.2f}"
                )
            with cols[1]:
                stake = prepared["risk_approved_stake"]
                approve_disabled = not (config.live_trading_enabled and st.session_state.live_mode_on)
                if st.button(
                    f":material/check_circle: APPROVE REAL ${stake:.2f} TRADE",
                    key=f"approve_{prepared['prepared_order_id']}", type="primary", disabled=approve_disabled,
                ):
                    _approve_and_execute(conn, prepared["prepared_order_id"], config)
                if approve_disabled:
                    st.caption("Flip LIVE MODE on above (and LIVE_TRADING_ENABLED in config) to enable this.")
                if st.button(":material/cancel: Reject", key=f"reject_{prepared['prepared_order_id']}"):
                    approval.reject(conn, prepared["prepared_order_id"], "rejected from Streamlit")
                    st.rerun()


def _approve_and_execute(conn: Any, prepared_order_id: int, config: Any) -> None:
    """One click does both steps described in the plan: create the
    ExecutionAuthorization, then immediately run the full fresh
    revalidation + (only if everything still passes) submission --
    never a blind resubmission of stale numbers. Consuming the prepared
    order via an atomic UPDATE inside approval.approve() is what makes
    a Streamlit rerun/double-click harmless -- a second click here
    simply finds the prepared order no longer READY."""
    authorization = approval.approve(conn, prepared_order_id, config)
    if authorization is None:
        st.error("Could not approve -- this prepared order was already claimed, expired, or rejected.")
        st.rerun()
        return

    try:
        provider = get_provider(authorization.provider, config)
    except Exception as exc:
        st.error(f"Could not initialize provider: {type(exc).__name__}: {exc}")
        st.rerun()
        return

    result = service.execute_authorized(conn, authorization.approval_id, provider, config)
    if result.outcome == "SUBMITTED":
        st.success(f"Order submitted. {result.detail}")
    elif result.outcome == "INVALIDATED":
        st.warning(f"Approval invalidated on revalidation: {result.reason} -- {result.detail}")
    else:
        st.error(f"Blocked: {result.reason} -- {result.detail}")
    st.rerun()


def _render_approved_awaiting_execution(conn: Any, config: Any) -> None:
    st.markdown("### APPROVED / EXECUTING")
    rows = conn.execute("SELECT * FROM execution_authorizations WHERE status = 'APPROVED'").fetchall()
    if not rows:
        st.caption("None. (Normally empty -- approving immediately attempts execution in this UI.)")
        return
    for row in rows:
        st.markdown(f"- {row['provider'].upper()} · stake=${row['approved_stake_usd']:.2f} · expires {row['expires_at']}")


def _render_recent_activity(conn: Any) -> None:
    st.markdown("### RECENT ACTIVITY")
    activity_tabs = st.tabs(["FILLED", "INVALIDATED", "REJECTED", "EXPIRED"])

    with activity_tabs[0]:
        rows = conn.execute(
            "SELECT * FROM live_orders WHERE status IN ('FILLED', 'PARTIALLY_FILLED', 'SUBMITTED', 'ACCEPTED') "
            "ORDER BY live_order_id DESC LIMIT 20"
        ).fetchall()
        _render_order_rows(rows)

    with activity_tabs[1]:
        rows = conn.execute(
            "SELECT * FROM execution_authorizations WHERE status = 'INVALIDATED' ORDER BY approval_id DESC LIMIT 20"
        ).fetchall()
        for row in rows:
            st.markdown(f"- {row['provider'].upper()} · {row['invalidation_reason']} · {row['invalidated_at']}")
        if not rows:
            st.caption("None.")

    with activity_tabs[2]:
        rows = conn.execute(
            "SELECT * FROM prepared_live_orders WHERE status = 'REJECTED' ORDER BY prepared_order_id DESC LIMIT 20"
        ).fetchall()
        for row in rows:
            st.markdown(f"- {row['event']} ({row['provider']})")
        if not rows:
            st.caption("None.")

    with activity_tabs[3]:
        rows = conn.execute(
            "SELECT * FROM prepared_live_orders WHERE status = 'EXPIRED' ORDER BY prepared_order_id DESC LIMIT 20"
        ).fetchall()
        for row in rows:
            st.markdown(f"- {row['event']} ({row['provider']})")
        if not rows:
            st.caption("None.")


def _render_order_rows(rows: list) -> None:
    if not rows:
        st.caption("None.")
        return
    for row in rows:
        st.markdown(
            f"- **{row['status']}** {row['provider'].upper()} {row['side']} "
            f"qty_filled={row['quantity_filled']}/{row['quantity_requested']} "
            f"@ {row['average_fill_price']} · order_id={row['provider_order_id']}"
        )
