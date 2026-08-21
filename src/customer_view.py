"""Read-only customer-facing VIP product view (MLB, NFL, WNBA).

Public requests never query protected upcoming recommendation fields. The
temporary entitlement adapter uses a server-side staging token so a future
billing provider can replace one function without changing the UI contract.
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

from database.db_manager import get_connection, init_db, get_performance_baseline
from src.grading import performance_summary, breakdown_by_field, assign_bucket, EV_BUCKETS

logger = logging.getLogger(__name__)


st.set_page_config(page_title="VIP | Sharp Market Intelligence", page_icon="🎯", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=Playfair+Display:ital,wght@0,700;1,700&display=swap');
:root {
  --ink:#f5f1e6; --muted:#9a9488; --line:#2c2a22; --panel:#141310;
  --gold:#e8b923; --gold-soft:#caa23a; --win:#3ddc84; --loss:#ff5468; --ref:#b3a687;
}
.stApp { background: radial-gradient(circle at 85% 0%, #211b0c 0, #0d0b07 38%, #080705 100%); color:var(--ink); }
[data-testid="stHeader"] { background:rgba(8,7,5,.75); }
h1,h2,h3 { font-family:'Space Grotesk',sans-serif !important; letter-spacing:-.045em; color:var(--ink) !important; }
p,div,span,button { font-family:'DM Sans',sans-serif; }
.hero { padding:2.3rem 0 1.5rem; }
.eyebrow { color:var(--gold); font-weight:700; letter-spacing:.16em; font-size:.7rem; text-transform:uppercase; }
.hero h1 { font-family:'Playfair Display',serif !important; font-style:italic; font-weight:700 !important; font-size:clamp(2.6rem,6.4vw,5.6rem); line-height:1.08; margin:.55rem 0 1.1rem; letter-spacing:-.01em !important; }
.hero h1 em { color:var(--gold); font-style:italic; }
.hero p { color:var(--muted); font-size:1.05rem; max-width:680px; line-height:1.65; }
.pill { display:inline-block; padding:.42rem .72rem; border:1px solid var(--gold-soft); border-radius:999px; color:var(--gold); font-size:.72rem; font-weight:700; letter-spacing:.08em; }
.pick { background:linear-gradient(135deg,#1c1810,#120f0a); border:1px solid var(--line); border-radius:20px; padding:1.15rem 1.25rem; margin:.65rem 0; box-shadow:0 16px 38px rgba(0,0,0,.3); }
.pick.settled { border-color:#3a3320; }
.pick.win { background:linear-gradient(135deg,#122a1c,#0d1d15); border-color:#2f9e72; }
.pick.loss { background:linear-gradient(135deg,#2c151c,#1c1013); border-color:#c94b5c; }
.pick.push, .pick.void { background:linear-gradient(135deg,#221f19,#161410); border-color:#5c5646; }
.pick.locked { background:linear-gradient(135deg,#221c10,#15120b); border-color:#5c4c22; }
.pick.research { background:#18150e; border-color:#5d4e2c; }
.pick-title { font-family:'Space Grotesk'; font-size:1.18rem; font-weight:700; color:var(--ink); }
.pick-meta { color:var(--muted); font-size:.88rem; margin-top:.4rem; }
.edge { color:var(--gold); font-weight:700; }
.result-win { color:var(--win); font-weight:800; letter-spacing:.04em; }
.result-loss { color:var(--loss); font-weight:800; letter-spacing:.04em; }
.unit-line { color:var(--ink); font-family:'Space Grotesk'; font-size:1rem; font-weight:700; margin-top:.55rem; }
.gold { color:var(--gold); font-weight:700; }
.section-note { color:var(--muted); font-size:.9rem; line-height:1.5; }
.lock-copy { color:#e2dbc8; font-family:'Space Grotesk'; font-weight:600; letter-spacing:.02em; }
.feature { background:rgba(23,21,16,.72); border:1px solid var(--line); border-radius:16px; padding:1rem; min-height:120px; }
.feature-title { color:var(--gold); font-weight:700; font-size:.82rem; letter-spacing:.08em; text-transform:uppercase; }
.results-panel { background:linear-gradient(135deg,#191509,#100d07); border:1px solid var(--line); border-radius:20px; padding:1.4rem 1.5rem 1.1rem; margin:.8rem 0 1.2rem; }
.hero-checklist { margin:.9rem 0 1.4rem; }
.check-item { color:var(--ink); font-size:.98rem; margin:.45rem 0; display:flex; align-items:center; gap:.6rem; }
.check-mark { display:inline-flex; align-items:center; justify-content:center; width:1.3rem; height:1.3rem; border-radius:50%; border:1px solid var(--gold-soft); color:var(--gold); font-size:.72rem; font-weight:800; flex:none; }
.hero-cta { margin:.3rem 0 1.4rem; display:flex; gap:.75rem; flex-wrap:wrap; }
.btn-primary { background:var(--gold); color:#151006; font-weight:800; padding:.72rem 1.35rem; border-radius:10px; text-decoration:none; font-size:.92rem; display:inline-block; }
.btn-secondary { background:transparent; color:var(--ink); border:1px solid var(--line); font-weight:700; padding:.68rem 1.3rem; border-radius:10px; text-decoration:none; font-size:.92rem; display:inline-block; }
.footer-band { border-top:1px solid var(--line); padding:1.6rem 0 .4rem; margin-top:.6rem; }
.footer-label { color:var(--muted); font-size:.7rem; letter-spacing:.14em; text-transform:uppercase; font-weight:700; }
.footer-books { color:var(--ink); font-size:.95rem; margin-top:.5rem; letter-spacing:.01em; opacity:.85; }
.results-eyebrow { color:var(--gold); font-weight:700; letter-spacing:.14em; font-size:.68rem; text-transform:uppercase; }
.results-number { font-family:'Space Grotesk',sans-serif; font-size:3rem; font-weight:700; line-height:1.05; margin:.3rem 0 .2rem; }
.results-caption { color:var(--muted); font-size:.85rem; max-width:520px; line-height:1.5; }
</style>
""", unsafe_allow_html=True)


def _authorized_request() -> bool:
    """Staging entitlement adapter; replace with billing webhook/provider later."""
    expected = os.getenv("MLB_CUSTOMER_ACCESS_TOKEN", "")
    supplied = st.query_params.get("access", "")
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _market_label(value: str) -> str:
    return (value or "").replace("_ou", "").replace("_yn", "").replace("_", " ").title()


_LEAGUE_EMOJI = {"MLB": "⚾", "NFL": "🏈", "WNBA": "🏀"}


def _league_badge(pick: dict) -> str:
    league = (pick.get("league") or "MLB").upper()
    return f"{_LEAGUE_EMOJI.get(league, '')} {league}".strip()


def _fair_odds_label(pick: dict) -> str:
    fair = pick.get("fair_american_odds")
    return f"{fair:+d}" if isinstance(fair, int) else ("—" if fair is None else f"{fair:+.0f}")


def _confidence_label(pick: dict) -> str:
    grade = pick.get("confidence_grade")
    score = pick.get("confidence_score")
    if grade and score is not None:
        return f"{grade} ({score:.0f})"
    return grade or "—"


def _settled_status(row: dict) -> str:
    return (row.get("settlement_status") or row.get("outcome") or "").upper()


def _side_line_label(pick: dict) -> str:
    side = (pick.get("side") or "").title()
    market = pick.get("market_type") or ""
    if pick.get("line") is not None:
        return f"{side} {pick['line']}"
    if market == "batting_hits_yn":
        return f"{side} · 1+ hit"
    if market == "batting_homeRuns_yn":
        return f"{side} · 1+ home run"
    if market == "batting_stolenBases_yn":
        return f"{side} · 1+ stolen base"
    if market == "pitching_strikeouts_yn":
        return f"{side} · 1+ strikeout"
    if market == "pitching_earnedRuns_yn":
        return f"{side} · 1+ earned run"
    if market == "pitching_win_yn":
        return f"{side} · pitcher win"
    return side


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
                   hr.sport, hr.league, hr.fair_american_odds,
                   hr.confidence_score, hr.confidence_grade, hr.market_quality,
                   ms.settlement_status, ms.final_stat_value,
                   bu.profit_units, bu.risk_units,
                   cp.clv_probability, cp.closing_american, cp.closing_line,
                   cp.line_movement_direction
            FROM official_picks op
            JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
            JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
            LEFT JOIN bet_units bu ON bu.recommendation_id = hr.recommendation_id
            LEFT JOIN closing_prices cp ON cp.recommendation_id = hr.recommendation_id
            WHERE ms.settlement_status IN ('WIN','LOSS','PUSH','VOID','CANCELLED')
              AND op.pick_status = 'ACTIVE'
              AND hr.scan_timestamp >= ?
            ORDER BY hr.scan_timestamp DESC
        """, (baseline,)).fetchall()

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
              AND op.pick_status = 'ACTIVE'
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
                       hr.sport, hr.league, hr.fair_american_odds,
                       hr.confidence_score, hr.confidence_grade, hr.market_quality,
                       op.outcome, op.official_rank
                FROM official_picks op
                JOIN historical_recommendations hr ON hr.recommendation_id = op.recommendation_id
                LEFT JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
                WHERE hr.event_start_time IS NOT NULL
                  AND hr.event_start_time >= ? AND hr.event_start_time <= ?
                  AND op.pick_status = 'ACTIVE'
                  AND (ms.recommendation_id IS NULL
                       OR ms.settlement_status IN ('UNRESOLVED','ungraded'))
                ORDER BY hr.event_start_time, op.official_rank
            """, (now_iso, horizon_iso)).fetchall()
        research = []
        if authorized:
            research = conn.execute("""
                SELECT player_name, matchup, market_type, side, line, sportsbook,
                       offered_american_odds, ev_pct, yn_implied_prob_adv,
                       model_score, event_start_time, sport, league,
                       fair_american_odds, confidence_score, confidence_grade,
                       market_quality
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


def _apply_filters(rows: list[dict], filters: dict) -> list[dict]:
    """Filter a list of pick dicts by sport/sportsbook/market/EV/confidence/date."""
    out = rows
    if filters.get("sports"):
        wanted = set(filters["sports"])
        out = [r for r in out if (r.get("league") or "MLB").upper() in wanted]
    if filters.get("sportsbooks"):
        wanted = {b.lower() for b in filters["sportsbooks"]}
        out = [r for r in out if (r.get("sportsbook") or "").lower() in wanted]
    if filters.get("markets"):
        wanted = set(filters["markets"])
        out = [r for r in out if r.get("market_type") in wanted]
    if filters.get("min_ev") is not None:
        min_ev = filters["min_ev"]
        out = [r for r in out if (r.get("ev_pct") is None or r["ev_pct"] >= min_ev)]
    if filters.get("confidence_grades"):
        wanted = set(filters["confidence_grades"])
        out = [r for r in out if (r.get("confidence_grade") or "—") in wanted]
    if filters.get("date_from"):
        out = [r for r in out if (r.get("scan_timestamp") or "") >= filters["date_from"]]
    if filters.get("date_to"):
        out = [r for r in out if (r.get("scan_timestamp") or "") <= filters["date_to"]]
    return out


def render_pick_filters(rows: list[dict], key_prefix: str) -> dict:
    """Render a filter bar over the given pool of picks and return selections."""
    sports = sorted({(r.get("league") or "MLB").upper() for r in rows})
    books = sorted({r.get("sportsbook") for r in rows if r.get("sportsbook")})
    markets = sorted({r.get("market_type") for r in rows if r.get("market_type")})
    grades = sorted({r.get("confidence_grade") for r in rows if r.get("confidence_grade")})

    cols = st.columns(4)
    with cols[0]:
        f_sports = st.multiselect("Sport", sports, default=sports, key=f"{key_prefix}_sports")
    with cols[1]:
        f_books = st.multiselect("Sportsbook", books, key=f"{key_prefix}_books")
    with cols[2]:
        f_markets = st.multiselect("Market", markets, key=f"{key_prefix}_markets",
                                    format_func=_market_label)
    with cols[3]:
        f_grades = st.multiselect("Confidence", grades, key=f"{key_prefix}_grades")
    f_min_ev = st.slider("Minimum EV%", -5.0, 20.0, -5.0, 0.5, key=f"{key_prefix}_ev")

    return {
        "sports": f_sports, "sportsbooks": f_books, "markets": f_markets,
        "confidence_grades": f_grades,
        "min_ev": f_min_ev if f_min_ev > -5.0 else None,
    }


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
    side_line = _side_line_label(pick)
    market = pick.get("market_type") or ""
    if market.endswith("_yn"):
        advantage = pick.get("yn_implied_prob_adv")
        edge_text = f"{advantage:+.2f} pp price advantage" if advantage is not None else "Price advantage tracked"
    else:
        edge = pick.get("ev_pct")
        edge_text = f"{edge:+.2f}% EV" if edge is not None else "Edge tracked"
    status = _settled_status(pick) or "OPEN"
    result_class = status.lower() if status.lower() in {"win", "loss", "push", "void"} else ""
    final = f" · Final: {pick['final_stat_value']}" if pick.get("final_stat_value") is not None else ""
    units = f" · {pick['profit_units']:+.2f}u" if pick.get("profit_units") is not None else ""
    stake = f"Stake: {pick['risk_units']:.2f}u" if pick.get("risk_units") is not None else "Stake: —"
    result_label = status if status in {"WIN", "LOSS", "PUSH", "VOID", "CANCELLED"} else "OPEN"
    result_style = "result-win" if status == "WIN" else "result-loss" if status == "LOSS" else ""

    detail_bits = [
        f"Fair odds: {_fair_odds_label(pick)}",
        f"Confidence: {_confidence_label(pick)}",
    ]
    if pick.get("market_quality"):
        detail_bits.append(f"Market: {_market_label(pick['market_quality'])}")
    detail_line = " · ".join(detail_bits)

    closing_bits = []
    if settled:
        clv = pick.get("clv_probability")
        if clv is not None:
            closing_bits.append(f"CLV: {clv:+.2%}")
        elif pick.get("line_movement_direction"):
            closing_bits.append(f"Line moved: {pick['line_movement_direction']}")
        if pick.get("closing_american") is not None:
            closing_line = pick.get("closing_line")
            close_txt = f"Closed {closing_line} {pick['closing_american']:+d}" if closing_line is not None \
                else f"Closed {pick['closing_american']:+d}"
            closing_bits.append(close_txt)
    closing_line_html = f'<div class="pick-meta">{" · ".join(closing_bits)}</div>' if closing_bits else ""

    st.markdown(f"""
    <div class="pick {'settled' if settled else ''} {result_class}">
      <div class="pick-title">{pick.get('player_name') or 'Official Play'}</div>
      <div class="pick-meta">{_league_badge(pick)} · {pick.get('matchup','')} · {_market_label(pick.get('market_type',''))} · {side_line}</div>
      <div class="pick-meta">{pick.get('sportsbook','')} {pick.get('offered_american_odds','')} · <span class="edge">{edge_text}</span></div>
      <div class="pick-meta">{detail_line}</div>
      {closing_line_html}
      <div class="unit-line">{stake} · Result: <span class="{result_style}">{result_label}{final}{units}</span></div>
    </div>
    """, unsafe_allow_html=True)


def _render_locked_pick(lock: dict) -> None:
    st.markdown(f"""
    <div class="pick locked">
      <div class="pick-title">{lock.get('matchup') or 'Game'}</div>
      <div class="pick-meta">{lock.get('event_start_time','')[:16]} · Official Model Play</div>
      <div class="lock-copy">VIP PICK AVAILABLE 🔒</div>
      <div class="pick-meta">Unlock the exact player and wager before first pitch.</div>
    </div>
    """, unsafe_allow_html=True)


authorized = _authorized_request()
try:
    data = load_customer_data(authorized)
except Exception:
    logger.exception("Customer data load failed")
    st.error("The model data is temporarily unavailable. Please check back shortly.")
    st.stop()

today = datetime.now(timezone.utc).strftime("%B %d, %Y")
st.markdown(f"""
<div class="hero">
  <div class="eyebrow">VIP · Sharp Market Intelligence · MLB · NFL · WNBA</div>
  <h1>Stop guessing.<br><em>Find the number.</em></h1>
  <p>Thousands of sportsbook prices are screened for fair value, market quality, and closing-line evidence. The model does not need a play every day.</p>
  <div class="hero-checklist">
    <div class="check-item"><span class="check-mark">&#10003;</span> Every price checked against Pinnacle and the wider market</div>
    <div class="check-item"><span class="check-mark">&#10003;</span> Closing-line value tracked on every settled pick</div>
    <div class="check-item"><span class="check-mark">&#10003;</span> Wins and losses shown equally &mdash; nothing hidden</div>
  </div>
  <div class="hero-cta">
    <a href="#today-picks" class="btn-primary">View Today's Picks &rarr;</a>
    <a href="#track-record" class="btn-secondary">See the Track Record</a>
  </div>
  <span class="pill">{today} · {'SUBSCRIBER VIEW' if authorized else 'PUBLIC VIEW'}</span>
</div>
<div id="today-picks"></div>
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
    st.subheader("Today's Official Picks — Upcoming")
    if data["upcoming"]:
        with st.expander("Filter upcoming picks", expanded=False):
            up_filters = render_pick_filters(data["upcoming"], "upcoming")
        filtered_upcoming = _apply_filters(data["upcoming"], up_filters)
        if filtered_upcoming:
            for pick in filtered_upcoming:
                _render_full_pick(pick)
        else:
            st.caption("No upcoming picks match the current filters.")
    else:
        st.success("No Official Plays Yet")
        st.caption("The model has not identified an opportunity meeting today's qualification standards.")
    if data["research"]:
        with st.expander("Research Opportunities"):
            for pick in data["research"]:
                _render_full_pick(pick)

st.divider()
st.markdown('<div id="track-record"></div>', unsafe_allow_html=True)
st.subheader("Verified Track Record — Past Picks")
st.caption("Settled Official Picks only. Winners and losses are included equally; no results are manually selected or hidden.")
if data["settled"]:
    with st.expander("Filter settled picks", expanded=False):
        settled_filters = render_pick_filters(data["settled"], "settled")
    filtered_settled = _apply_filters(data["settled"], settled_filters)

    summary = performance_summary(filtered_settled)
    period = st.radio("Performance period", ["7D", "30D", "ALL"], horizontal=True, index=2)
    series = performance_series(filtered_settled, period)
    if not series.empty:
        chart_df = series.rename(columns={
            "expected_cumulative": "Expected Units",
            "actual_cumulative": "Actual Units",
        }).reset_index().rename(columns={"posted": "Date"})

        period_units = chart_df["Actual Units"].iloc[-1]
        positive = period_units >= 0
        line_color = "#3ddc84" if positive else "#ff5468"

        st.markdown(f"""
        <div class="results-panel">
          <div class="results-eyebrow">Real Results — {period}</div>
          <div class="results-number" style="color:{line_color};">{period_units:+.2f}u</div>
          <div class="results-caption">Cumulative result if every Official Pick were followed at its recorded stake.
          Every settled pick counts — wins and losses included equally, nothing hidden or cherry-picked.</div>
        </div>
        """, unsafe_allow_html=True)

        area = alt.Chart(chart_df).mark_area(
            line={"color": line_color, "strokeWidth": 2.5},
            color=line_color, opacity=0.16, interpolate="monotone",
        ).encode(
            x=alt.X("Date:T", title=None,
                    axis=alt.Axis(grid=False, labelColor="#9a9488", tickColor="#2c2a22", domainColor="#2c2a22")),
            y=alt.Y("Actual Units:Q", title="Cumulative units",
                    axis=alt.Axis(grid=True, gridColor="#211d14", labelColor="#9a9488", titleColor="#9a9488")),
            tooltip=[alt.Tooltip("Date:T", title="Date"), alt.Tooltip("Actual Units:Q", format="+.2f")],
        )
        expected_line = alt.Chart(chart_df).mark_line(
            color="#b3a687", strokeDash=[4, 3], strokeWidth=1.6, interpolate="monotone", opacity=0.85,
        ).encode(
            x="Date:T",
            y="Expected Units:Q",
            tooltip=[alt.Tooltip("Date:T", title="Date"),
                     alt.Tooltip("Expected Units:Q", format="+.2f", title="Expected Units")],
        )
        st.caption("Solid area: actual settled profit. Dashed line: expected units from each pick's recorded EV and stake.")
        st.altair_chart(
            (area + expected_line).properties(height=300)
            .configure_view(strokeWidth=0)
            .configure(background="transparent"),
            width="stretch",
        )

    if filtered_settled:
        with st.expander(f"View all {len(filtered_settled)} settled picks", expanded=False):
            for pick in filtered_settled[:10]:
                _render_full_pick(pick, settled=True)
    else:
        st.caption("No settled picks match the current filters.")

    st.markdown("#### Performance Dashboard")
    cols = st.columns(6)
    cols[0].metric("Record", f"{summary['wins']}-{summary['losses']}-{summary['pushes']}")
    cols[1].metric("Settled Picks", summary["settled"])
    cols[2].metric("Units", f"{summary['units_won']:+.2f}")
    cols[3].metric("ROI", f"{summary['roi']:.1%}" if summary["units_risked"] else "—")
    cols[4].metric("Avg CLV", f"{summary['avg_clv_probability']:+.2%}" if summary["avg_clv_probability"] is not None else "—")
    cols[5].metric("Beat Close %", f"{summary['pct_beating_close']:.1%}" if summary["pct_beating_close"] is not None else "—")
    st.caption(f"Average EV at recommendation: {summary['avg_ev_pct']:+.2f}%")

    with st.expander("Performance breakdown by sport / market / sportsbook / confidence / EV"):
        for field, label in [("sport", "Sport"), ("market_type", "Market"),
                              ("sportsbook", "Sportsbook"), ("confidence_grade", "Confidence Grade")]:
            groups = breakdown_by_field(filtered_settled, field)
            if not groups:
                continue
            st.markdown(f"**By {label}**")
            table = pd.DataFrame([
                {label: key, "Record": f"{g['wins']}-{g['losses']}-{g['pushes']}",
                 "Units": round(g["units_won"], 2), "ROI": f"{g['roi']:.1%}" if g["units_risked"] else "—",
                 "Avg EV%": g["avg_ev_pct"], "Beat Close %":
                     f"{g['pct_beating_close']:.1%}" if g["pct_beating_close"] is not None else "—"}
                for key, g in sorted(groups.items())
            ])
            st.dataframe(table, hide_index=True, use_container_width=True)

        ev_bucketed = [{**r, "_ev_bucket": assign_bucket(r.get("ev_pct") or 0.0, EV_BUCKETS)}
                       for r in filtered_settled]
        ev_groups = breakdown_by_field(ev_bucketed, "_ev_bucket")
        if ev_groups:
            st.markdown("**By EV Bucket**")
            table = pd.DataFrame([
                {"EV Bucket": key, "Record": f"{g['wins']}-{g['losses']}-{g['pushes']}",
                 "Units": round(g["units_won"], 2), "ROI": f"{g['roi']:.1%}" if g["units_risked"] else "—"}
                for key, g in sorted(ev_groups.items())
            ])
            st.dataframe(table, hide_index=True, use_container_width=True)
else:
    st.info("BUILDING VERIFIED TRACK RECORD · Performance appears after official picks settle.")

st.divider()
features = st.columns(4)
for col, title, body in zip(features, ["Multi-book scan", "Fair value", "Sharp reference", "Accountability"], [
    "Prices compared across sportsbooks.", "Conservative probability and EV checks.", "Pinnacle used only on exact valid matches.", "Pregame prices, CLV, and results are preserved.",
]):
    with col:
        st.markdown(f'<div class="feature"><div class="feature-title">{title}</div><div class="section-note">{body}</div></div>', unsafe_allow_html=True)

_books_seen = sorted({
    (r.get("sportsbook") or "").strip()
    for pool in (data["settled"], data["upcoming"], data["research"])
    for r in pool
    if r.get("sportsbook")
})
_books_line = " &nbsp;·&nbsp; ".join(_books_seen) if _books_seen else "Books populate once the model has scanned live odds."

st.markdown(f"""
<div class="footer-band">
  <div class="footer-label">Leagues Covered &middot; Books Scanned &middot; Updated Automatically</div>
  <div class="footer-books">MLB &nbsp;·&nbsp; NFL &nbsp;·&nbsp; WNBA &nbsp;&mdash;&nbsp; {_books_line}</div>
</div>
""", unsafe_allow_html=True)

st.caption("This platform does not guarantee profit, place bets, or present Research opportunities as Official Picks.")
