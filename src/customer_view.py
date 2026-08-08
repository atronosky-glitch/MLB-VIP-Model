"""Read-only customer-facing MLB VIP view.

This app intentionally exposes no operational controls, credentials, raw
diagnostics, or internal thresholds. It reads the shared production database
and presents only frozen Official Picks, clearly separated Research, and
honest performance evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from database.db_manager import get_connection, init_db


st.set_page_config(page_title="MLB VIP | Sharp Market Intelligence", page_icon="⚾", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');
:root { --ink:#0b1220; --muted:#667085; --line:#e6eaf0; --mint:#c9f7df; --blue:#155eef; }
.stApp { background: #f7f9fc; color: var(--ink); }
[data-testid="stHeader"] { background: rgba(247,249,252,.88); }
h1,h2,h3 { font-family:'Space Grotesk', sans-serif !important; letter-spacing:-.04em; }
p,div,span,button { font-family:'DM Sans', sans-serif; }
.hero { padding: 2.5rem 0 1.4rem; }
.eyebrow { color:var(--blue); font-weight:700; letter-spacing:.14em; font-size:.72rem; text-transform:uppercase; }
.hero h1 { font-size:clamp(2.4rem,6vw,5.4rem); line-height:.95; margin:.45rem 0 1rem; }
.hero p { color:var(--muted); font-size:1.05rem; max-width:660px; }
.pill { display:inline-block; padding:.42rem .7rem; border-radius:999px; background:var(--mint); color:#087443; font-size:.75rem; font-weight:700; }
.pick { background:white; border:1px solid var(--line); border-radius:20px; padding:1.1rem 1.2rem; margin:.55rem 0; box-shadow:0 10px 30px rgba(16,24,40,.04); }
.pick-title { font-family:'Space Grotesk'; font-size:1.1rem; font-weight:700; }
.pick-meta { color:var(--muted); font-size:.88rem; margin-top:.35rem; }
.edge { color:#087443; font-weight:700; }
.research { background:#fffdf5; border-color:#f3e6b3; }
.section-note { color:var(--muted); font-size:.9rem; }
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=30, show_spinner=False)
def load_customer_data() -> tuple[list[dict], list[dict], list[dict]]:
    init_db()
    conn = get_connection()
    try:
        official = conn.execute("""
            SELECT hr.*, op.tier, op.official_rank, op.outcome,
                   op.profit_units AS official_profit_units,
                   ms.settlement_status, bu.profit_units, bu.risk_units
            FROM official_picks op
            JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
            LEFT JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
            LEFT JOIN bet_units bu ON bu.recommendation_id = hr.recommendation_id
            WHERE date(hr.scan_timestamp) = date('now')
            ORDER BY op.official_rank, hr.model_score DESC
        """).fetchall()
        research = conn.execute("""
            SELECT hr.*
            FROM historical_recommendations hr
            WHERE date(hr.scan_timestamp) = date('now')
              AND COALESCE(hr.recommendation_tier, 'RESEARCH_ONLY') <> 'OFFICIAL_TRACKED'
            ORDER BY hr.model_score DESC, hr.ev_pct DESC
            LIMIT 25
        """).fetchall()
        performance = conn.execute("""
            SELECT date(hr.scan_timestamp) AS day,
                   COUNT(*) AS settled,
                   SUM(CASE WHEN ms.settlement_status = 'WIN' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN ms.settlement_status = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                   SUM(COALESCE(bu.profit_units, 0)) AS profit_units,
                   SUM(COALESCE(bu.risk_units, 0)) AS risk_units,
                   AVG(hr.ev_pct) AS avg_ev
            FROM historical_recommendations hr
            JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
            LEFT JOIN bet_units bu ON bu.recommendation_id = hr.recommendation_id
            WHERE ms.settlement_status IN ('WIN','LOSS','PUSH','VOID','CANCELLED')
            GROUP BY date(hr.scan_timestamp)
            ORDER BY day
        """).fetchall()
        return [dict(r) for r in official], [dict(r) for r in research], [dict(r) for r in performance]
    finally:
        conn.close()


def _market_label(value: str) -> str:
    return (value or "").replace("_ou", "").replace("_yn", "").replace("_", " ").title()


def _render_pick(pick: dict, research: bool = False) -> None:
    side = pick.get("side", "")
    line = "" if pick.get("line") is None else f" {pick['line']}"
    edge = pick.get("ev_pct")
    edge_text = f"{edge:+.2f}% EV" if edge is not None and not research else "Price advantage tracked"
    status = (pick.get("settlement_status") or pick.get("outcome") or "OPEN").upper()
    cls = "pick research" if research else "pick"
    st.markdown(f"""
    <div class="{cls}">
      <div class="pick-title">{pick.get('player_name') or pick.get('matchup') or 'MLB Market'}</div>
      <div class="pick-meta">{_market_label(pick.get('market_type',''))} · {side.title()}{line} · {pick.get('sportsbook','')}</div>
      <div class="pick-meta">{pick.get('event_start_time','')[:16]} · {pick.get('offered_american_odds','')} · <span class="edge">{edge_text}</span> · {status}</div>
    </div>
    """, unsafe_allow_html=True)


official, research, performance = load_customer_data()
today = datetime.now(timezone.utc).strftime("%B %d, %Y")

st.markdown(f"""
<div class="hero">
  <div class="eyebrow">MLB VIP · Sharp Market Intelligence</div>
  <h1>Find the price.<br>Respect the edge.</h1>
  <p>Multi-sportsbook market comparison, conservative fair-value estimates, and transparent performance tracking. The model filters the slate instead of forcing a bet.</p>
  <span class="pill">SHADOW MODE · {today}</span>
</div>
""", unsafe_allow_html=True)

st.subheader("Today's Official Picks")
st.markdown('<div class="section-note">Frozen opportunities that passed every production safety gate. No pick is shown when the evidence is not strong enough.</div>', unsafe_allow_html=True)
if official:
    for pick in official:
        _render_pick(pick)
else:
    st.info("No Official Picks today. That means the slate did not produce a fully qualified opportunity.")

st.divider()
st.subheader("Research Opportunities")
st.markdown('<div class="section-note">Useful market signals that remain below the Official standard. Research is not a betting recommendation.</div>', unsafe_allow_html=True)
if research:
    for pick in research[:10]:
        _render_pick(pick, research=True)
else:
    st.info("No research opportunities are currently available.")

st.divider()
st.subheader("Performance Evidence")
if performance:
    frame = pd.DataFrame(performance)
    frame["roi"] = frame.apply(lambda r: r["profit_units"] / r["risk_units"] if r["risk_units"] else 0.0, axis=1)
    frame["expected_ev"] = frame["avg_ev"].fillna(0) / 100.0
    frame["realized_roi"] = frame["roi"]
    st.line_chart(frame.set_index("day")[["expected_ev", "realized_roi"]], y_label="Expected EV / Realized ROI")
    metrics = st.columns(4)
    metrics[0].metric("Settled Picks", int(frame["settled"].sum()))
    metrics[1].metric("Wins", int(frame["wins"].sum()))
    metrics[2].metric("Losses", int(frame["losses"].sum()))
    metrics[3].metric("Units", f"{frame['profit_units'].sum():+.2f}")
else:
    st.info("Performance chart is awaiting settled sample data. No historical results are fabricated.")

st.caption("The model does not guarantee profit, place bets, or treat research opportunities as Official Picks.")
