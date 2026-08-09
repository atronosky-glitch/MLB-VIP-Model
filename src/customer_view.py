"""Read-only customer-facing MLB VIP product view.

Public requests never query protected upcoming recommendation fields. The
temporary entitlement adapter uses a server-side staging token so a future
billing provider can replace one function without changing the UI contract.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from database.db_manager import get_connection, init_db, get_performance_baseline


st.set_page_config(page_title="MLB VIP | Sharp Market Intelligence", page_icon="⚾", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
:root { --ink:#e8eef7; --muted:#9aa9bc; --line:#263448; --panel:#111b2b; --mint:#9ef0c7; --blue:#83aaff; --gold:#f2c66d; }
.stApp { background: radial-gradient(circle at 85% 0%, #172b4a 0, #08111f 38%, #060c16 100%); color:var(--ink); }
[data-testid="stHeader"] { background:rgba(6,12,22,.75); }
h1,h2,h3 { font-family:'Space Grotesk',sans-serif !important; letter-spacing:-.045em; color:var(--ink) !important; }
p,div,span,button { font-family:'DM Sans',sans-serif; }
.hero { padding:2.3rem 0 1.5rem; }
.eyebrow { color:var(--mint); font-weight:700; letter-spacing:.16em; font-size:.7rem; text-transform:uppercase; }
.hero h1 { font-size:clamp(2.8rem,7vw,6.4rem); line-height:.9; margin:.55rem 0 1.1rem; }
.hero p { color:var(--muted); font-size:1.05rem; max-width:680px; line-height:1.65; }
.pill { display:inline-block; padding:.42rem .72rem; border:1px solid #31506b; border-radius:999px; color:var(--mint); font-size:.72rem; font-weight:700; letter-spacing:.08em; }
.pick { background:linear-gradient(135deg,#13233a,#0e1828); border:1px solid var(--line); border-radius:20px; padding:1.15rem 1.25rem; margin:.65rem 0; box-shadow:0 16px 38px rgba(0,0,0,.18); }
.pick.settled { border-color:#2d6e57; }
.pick.win { background:linear-gradient(135deg,#102d26,#0d1d1b); border-color:#2f9e72; }
.pick.loss { background:linear-gradient(135deg,#321b25,#20131a); border-color:#d45b6b; }
.pick.push, .pick.void { background:linear-gradient(135deg,#252a35,#171b24); border-color:#667085; }
.pick.locked { background:linear-gradient(135deg,#192238,#101827); border-color:#3b4e6e; }
.pick.research { background:#171b26; border-color:#5d4e2c; }
.pick-title { font-family:'Space Grotesk'; font-size:1.18rem; font-weight:700; color:var(--ink); }
.pick-meta { color:var(--muted); font-size:.88rem; margin-top:.4rem; }
.edge { color:var(--mint); font-weight:700; }
.result-win { color:#72efb2; font-weight:800; letter-spacing:.04em; }
.result-loss { color:#ff8c9a; font-weight:800; letter-spacing:.04em; }
.unit-line { color:#e8eef7; font-family:'Space Grotesk'; font-size:1rem; font-weight:700; margin-top:.55rem; }
.gold { color:var(--gold); font-weight:700; }
.section-note { color:var(--muted); font-size:.9rem; line-height:1.5; }
.lock-copy { color:#c6d1e1; font-family:'Space Grotesk'; font-weight:600; letter-spacing:.02em; }
.feature { background:rgba(17,27,43,.72); border:1px solid var(--line); border-radius:16px; padding:1rem; min-height:120px; }
.feature-title { color:var(--mint); font-weight:700; font-size:.82rem; letter-spacing:.08em; text-transform:uppercase; }
</style>
""", unsafe_allow_html=True)


def _authorized_request() -> bool:
    """Staging entitlement adapter; replace with billing webhook/provider later."""
    expected = os.getenv("MLB_CUSTOMER_ACCESS_TOKEN", "")
    supplied = st.query_params.get("access", "")
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _market_label(value: str) -> str:
    return (value or "").replace("_ou", "").replace("_yn", "").replace("_", " ").title()


def _settled_status(row: dict) -> str:
    return (row.get("settlement_status") or row.get("outcome") or "").upper()


def public_lock_view(row: dict) -> dict:
    """Project only non-sensitive pre-settlement fields for public display."""
    return {
        "matchup": row.get("matchup"),
        "event_start_time": row.get("event_start_time"),
        "event_status": row.get("event_status"),
        "scan_timestamp": row.get("scan_timestamp"),
        "official_rank": row.get("official_rank"),
    }


@st.cache_data(ttl=30, show_spinner=False)
def load_customer_data(authorized: bool) -> dict:
    """Load only fields allowed for the request's entitlement level."""
    init_db()
    conn = get_connection()
    now_iso = datetime.now(timezone.utc).isoformat()
    horizon_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    baseline = get_performance_baseline(conn)
    try:
        settled = conn.execute("""
            SELECT hr.player_name, hr.matchup, hr.market_type, hr.side, hr.line,
                   hr.sportsbook, hr.offered_american_odds, hr.ev_pct,
                   hr.model_score, hr.scan_timestamp, hr.event_start_time,
                   ms.settlement_status, ms.final_stat_value,
                   bu.profit_units, bu.risk_units, cp.clv_probability
            FROM official_picks op
            JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
            JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
            LEFT JOIN bet_units bu ON bu.recommendation_id = hr.recommendation_id
            LEFT JOIN closing_prices cp ON cp.recommendation_id = hr.recommendation_id
            WHERE ms.settlement_status IN ('WIN','LOSS','PUSH','VOID','CANCELLED')
              AND (? IS NULL OR hr.scan_timestamp >= ?)
            ORDER BY hr.scan_timestamp DESC
        """, (baseline, baseline)).fetchall()

        # This query intentionally contains no player, side, line, sportsbook,
        # odds, EV, or market fields. Public visitors only learn that a play
        # exists for a matchup and time.
        locked = conn.execute("""
            SELECT hr.matchup, hr.event_start_time, hr.event_status,
                   hr.scan_timestamp, op.official_rank
            FROM official_picks op
            JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
            LEFT JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
            WHERE hr.event_start_time IS NOT NULL
              AND hr.event_start_time >= ? AND hr.event_start_time <= ?
              AND (ms.recommendation_id IS NULL
                   OR ms.settlement_status IN ('UNRESOLVED','ungraded'))
            ORDER BY hr.event_start_time, op.official_rank
        """, (now_iso, horizon_iso)).fetchall()

        upcoming = []
        if authorized:
            upcoming = conn.execute("""
                SELECT hr.player_name, hr.matchup, hr.market_type, hr.side, hr.line,
                       hr.sportsbook, hr.offered_american_odds, hr.ev_pct,
                       hr.model_score, hr.scan_timestamp, hr.event_start_time,
                       op.outcome, op.official_rank
                FROM official_picks op
                JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
                LEFT JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
                WHERE hr.event_start_time IS NOT NULL
                  AND hr.event_start_time >= ? AND hr.event_start_time <= ?
                  AND (ms.recommendation_id IS NULL
                       OR ms.settlement_status IN ('UNRESOLVED','ungraded'))
                ORDER BY hr.event_start_time, op.official_rank
            """, (now_iso, horizon_iso)).fetchall()
        research = []
        if authorized:
            research = conn.execute("""
                SELECT player_name, matchup, market_type, side, line, sportsbook,
                       offered_american_odds, ev_pct, yn_implied_prob_adv,
                       model_score, event_start_time
                FROM historical_recommendations
                WHERE date(scan_timestamp) = date('now')
                  AND COALESCE(recommendation_tier, 'RESEARCH_ONLY') <> 'OFFICIAL_TRACKED'
                ORDER BY model_score DESC, ev_pct DESC
                LIMIT 25
            """).fetchall()
        return {
            "settled": [dict(r) for r in settled],
            "locked": [dict(r) for r in locked],
            "upcoming": [dict(r) for r in upcoming],
            "research": [dict(r) for r in research],
        }
    finally:
        conn.close()


def performance_series(rows: list[dict], period: str = "ALL") -> pd.DataFrame:
    """Calculate cumulative expected units versus actual units."""
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["posted"] = pd.to_datetime(frame["scan_timestamp"], utc=True, errors="coerce")
    frame = frame.dropna(subset=["posted"]).sort_values("posted")
    if period in ("7D", "30D"):
        days = 7 if period == "7D" else 30
        frame = frame[frame["posted"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)]
    frame["risk_units"] = pd.to_numeric(frame["risk_units"], errors="coerce").fillna(0.0)
    frame["profit_units"] = pd.to_numeric(frame["profit_units"], errors="coerce").fillna(0.0)
    frame["ev_pct"] = pd.to_numeric(frame["ev_pct"], errors="coerce").fillna(0.0)
    frame["expected_units"] = frame["risk_units"] * frame["ev_pct"] / 100.0
    frame["expected_cumulative"] = frame["expected_units"].cumsum()
    frame["actual_cumulative"] = frame["profit_units"].cumsum()
    return frame.set_index("posted")[["expected_cumulative", "actual_cumulative"]]


def _render_full_pick(pick: dict, settled: bool = False) -> None:
    side = pick.get("side", "")
    line = "" if pick.get("line") is None else f" {pick['line']}"
    edge = pick.get("ev_pct")
    edge_text = f"{edge:+.2f}% EV" if edge is not None else "Edge tracked"
    status = _settled_status(pick) or "OPEN"
    result_class = status.lower() if status.lower() in {"win", "loss", "push", "void"} else ""
    final = f" · Final: {pick['final_stat_value']}" if pick.get("final_stat_value") is not None else ""
    units = f" · {pick['profit_units']:+.2f}u" if pick.get("profit_units") is not None else ""
    stake = f"Stake: {pick['risk_units']:.2f}u" if pick.get("risk_units") is not None else "Stake: —"
    result_label = status if status in {"WIN", "LOSS", "PUSH", "VOID", "CANCELLED"} else "OPEN"
    result_style = "result-win" if status == "WIN" else "result-loss" if status == "LOSS" else ""
    st.markdown(f"""
    <div class="pick {'settled' if settled else ''} {result_class}">
      <div class="pick-title">{pick.get('player_name') or 'MLB Official Play'}</div>
      <div class="pick-meta">{pick.get('matchup','')} · {_market_label(pick.get('market_type',''))} · {side.title()}{line}</div>
      <div class="pick-meta">{pick.get('sportsbook','')} {pick.get('offered_american_odds','')} · <span class="edge">{edge_text}</span></div>
      <div class="unit-line">{stake} · Result: <span class="{result_style}">{result_label}{final}{units}</span></div>
    </div>
    """, unsafe_allow_html=True)


def _render_locked_pick(lock: dict) -> None:
    st.markdown(f"""
    <div class="pick locked">
      <div class="pick-title">{lock.get('matchup') or 'MLB Game'}</div>
      <div class="pick-meta">{lock.get('event_start_time','')[:16]} · Official Model Play</div>
      <div class="lock-copy">VIP PICK AVAILABLE 🔒</div>
      <div class="pick-meta">Unlock the exact player and wager before first pitch.</div>
    </div>
    """, unsafe_allow_html=True)


authorized = _authorized_request()
try:
    data = load_customer_data(authorized)
except Exception:
    st.error("The model data is temporarily unavailable. Please check back shortly.")
    st.stop()

today = datetime.now(timezone.utc).strftime("%B %d, %Y")
st.markdown(f"""
<div class="hero">
  <div class="eyebrow">MLB VIP · Sharp Market Intelligence</div>
  <h1>Stop guessing.<br>Find the number.</h1>
  <p>Thousands of sportsbook prices are screened for fair value, market quality, and closing-line evidence. The model does not need a play every day.</p>
  <span class="pill">{today} · {'SUBSCRIBER VIEW' if authorized else 'PUBLIC VIEW'}</span>
</div>
""", unsafe_allow_html=True)

if not authorized:
    st.info("Official plays are posted when the slate qualifies. Subscriber access unlocks the exact wager before the game; settled picks become public automatically for full accountability.")
    st.subheader("Today's Official Picks")
    if data["locked"]:
        for lock in data["locked"]:
            _render_locked_pick(lock)
        st.button("Unlock Today's Picks", type="primary", use_container_width=True, disabled=True)
    else:
        st.success("No Official Plays Yet")
        st.caption("The model has not identified an opportunity meeting today's qualification standards.")
else:
    st.subheader("Today's Official Picks")
    if data["upcoming"]:
        for pick in data["upcoming"]:
            _render_full_pick(pick)
    else:
        st.success("No Official Plays Yet")
        st.caption("The model has not identified an opportunity meeting today's qualification standards.")
    if data["research"]:
        with st.expander("Research Opportunities"):
            for pick in data["research"]:
                _render_full_pick(pick)

st.divider()
st.subheader("Verified Track Record")
st.caption("Settled Official Picks only. Winners and losses are included equally; no results are manually selected.")
if data["settled"]:
    for pick in data["settled"][:5]:
        _render_full_pick(pick, settled=True)
    period = st.radio("Performance period", ["7D", "30D", "ALL"], horizontal=True, index=2)
    series = performance_series(data["settled"], period)
    if not series.empty:
        chart = series.rename(columns={
            "expected_cumulative": "Expected Units",
            "actual_cumulative": "Actual Units",
        })
        st.caption("Expected Units use each pick's recorded EV and stake. Actual Units use canonical settled profit.")
        st.line_chart(chart, y_label="Cumulative units", height=300, use_container_width=True)
        raw = pd.DataFrame(data["settled"])
        profit = pd.to_numeric(raw["profit_units"], errors="coerce").fillna(0).sum()
        risk = pd.to_numeric(raw["risk_units"], errors="coerce").fillna(0).sum()
        wins = sum(_settled_status(r) == "WIN" for r in data["settled"])
        losses = sum(_settled_status(r) == "LOSS" for r in data["settled"])
        cols = st.columns(4)
        cols[0].metric("Record", f"{wins}-{losses}")
        cols[1].metric("Settled Picks", len(raw))
        cols[2].metric("Units", f"{profit:+.2f}")
        cols[3].metric("ROI", f"{profit / risk:.1%}" if risk else "-")
else:
    st.info("BUILDING VERIFIED TRACK RECORD · Performance appears after official picks settle.")

st.divider()
features = st.columns(4)
for col, title, body in zip(features, ["Multi-book scan", "Fair value", "Sharp reference", "Accountability"], [
    "Prices compared across sportsbooks.", "Conservative probability and EV checks.", "Pinnacle used only on exact valid matches.", "Pregame prices, CLV, and results are preserved.",
]):
    with col:
        st.markdown(f'<div class="feature"><div class="feature-title">{title}</div><div class="section-note">{body}</div></div>', unsafe_allow_html=True)

st.caption("MLB VIP does not guarantee profit, place bets, or present Research opportunities as Official Picks.")
