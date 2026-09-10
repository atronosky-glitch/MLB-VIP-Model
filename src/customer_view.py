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

from database.db_manager import (
    get_connection, init_db, get_performance_baseline, get_today_in_configured_timezone,
    format_event_start_local,
)
from src.grading import performance_summary, breakdown_by_field, assign_bucket, EV_BUCKETS
from src.sportsbook_picker import render_sportsbook_picker

logger = logging.getLogger(__name__)


st.set_page_config(page_title="VIP | Sharp Market Intelligence", page_icon="🎯", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Space+Grotesk:wght@500;600;700&display=swap');
:root {
  --ink:#111827; --muted:#6b7280; --line:#e5e7eb; --panel:#f9fafb;
  --accent:#111827; --accent-soft:#374151; --win:#16a34a; --loss:#dc2626; --ref:#9ca3af;
}
.stApp { background:#ffffff; color:var(--ink); }
[data-testid="stHeader"] { background:rgba(255,255,255,.92); }
h1,h2,h3 { font-family:'Space Grotesk',sans-serif !important; letter-spacing:-.02em; color:var(--ink) !important; }
p,div,span,button { font-family:'Inter',sans-serif; }
.topnav { display:flex; align-items:center; justify-content:space-between; padding:1.1rem 0; border-bottom:1px solid var(--line); flex-wrap:wrap; gap:.9rem; }
.topnav-brand { display:flex; align-items:center; gap:.65rem; }
.topnav-mark { display:inline-flex; align-items:center; justify-content:center; width:2.1rem; height:2.1rem; border-radius:8px; background:var(--accent); color:#fff; font-weight:800; font-family:'Space Grotesk'; font-size:.85rem; }
.topnav-word { color:var(--ink); font-weight:700; font-size:.95rem; letter-spacing:-.01em; }
.topnav-links { display:flex; gap:1.7rem; }
.topnav-links a { color:var(--muted); font-weight:700; font-size:.85rem; text-decoration:none; letter-spacing:.01em; }
.hero { padding:2.4rem 0 1.6rem; }
.eyebrow { color:var(--muted); font-weight:700; letter-spacing:.1em; font-size:.7rem; text-transform:uppercase; }
.hero h1 { font-weight:700 !important; font-size:clamp(2rem,4vw,2.9rem); line-height:1.2; margin:.6rem 0 .9rem; letter-spacing:-.02em !important; }
.hero p { color:var(--muted); font-size:1rem; max-width:680px; line-height:1.6; }
.pill { display:inline-block; padding:.4rem .7rem; border:1px solid var(--line); border-radius:6px; color:var(--muted); font-size:.72rem; font-weight:600; letter-spacing:.03em; }
.pick { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--line); border-radius:8px; padding:1.1rem 1.2rem; margin:.6rem 0; }
.pick.settled { border-left-color:var(--line); }
.pick.win { border-left-color:var(--win); }
.pick.loss { border-left-color:var(--loss); }
.pick.push, .pick.void { border-left-color:var(--ref); }
.pick.locked { border-left-color:var(--accent); }
.pick.research { border-left-color:var(--line); }
.pick-title { font-family:'Space Grotesk'; font-size:1.1rem; font-weight:700; color:var(--ink); }
.pick-meta { color:var(--muted); font-size:.87rem; margin-top:.35rem; }
.edge { color:var(--ink); font-weight:700; }
.result-win { color:var(--win); font-weight:700; }
.result-loss { color:var(--loss); font-weight:700; }
.unit-line { color:var(--ink); font-family:'Space Grotesk'; font-size:.98rem; font-weight:700; margin-top:.5rem; }
.gold { color:var(--ink); font-weight:700; }
.section-note { color:var(--muted); font-size:.9rem; line-height:1.5; }
.lock-copy { color:var(--ink); font-family:'Space Grotesk'; font-weight:600; letter-spacing:.01em; }
.feature { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:1rem; min-height:120px; }
.feature-title { color:var(--muted); font-weight:700; font-size:.8rem; letter-spacing:.05em; text-transform:uppercase; }
.results-panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:1.3rem 1.4rem 1rem; margin:.8rem 0 1.2rem; }
.hero-checklist { margin:.9rem 0 1.4rem; }
.check-item { color:var(--ink); font-size:.96rem; margin:.4rem 0; display:flex; align-items:center; gap:.6rem; }
.check-mark { display:inline-flex; align-items:center; justify-content:center; width:1.25rem; height:1.25rem; border-radius:4px; border:1px solid var(--accent); color:var(--accent); font-size:.7rem; font-weight:800; flex:none; }
.hero-cta { margin:.3rem 0 1.4rem; display:flex; gap:.75rem; flex-wrap:wrap; }
.btn-primary { background:var(--accent); color:#fff; font-weight:700; padding:.68rem 1.3rem; border-radius:6px; text-decoration:none; font-size:.9rem; display:inline-block; }
.btn-secondary { background:transparent; color:var(--ink); border:1px solid var(--line); font-weight:600; padding:.64rem 1.25rem; border-radius:6px; text-decoration:none; font-size:.9rem; display:inline-block; }
.footer-band { border-top:1px solid var(--line); padding:1.6rem 0 .4rem; margin-top:.6rem; }
.footer-label { color:var(--muted); font-size:.7rem; letter-spacing:.1em; text-transform:uppercase; font-weight:600; }
.footer-books { color:var(--ink); font-size:.92rem; margin-top:.5rem; letter-spacing:0; opacity:.8; }
.results-eyebrow { color:var(--muted); font-weight:700; letter-spacing:.1em; font-size:.68rem; text-transform:uppercase; }
.results-number { font-family:'Space Grotesk',sans-serif; font-size:2.5rem; font-weight:700; line-height:1.05; margin:.3rem 0 .2rem; }
.results-caption { color:var(--muted); font-size:.85rem; max-width:520px; line-height:1.5; }
/* Streamlit's own theme (.streamlit/config.toml) is dark with a bright
   lime primaryColor, shared with the admin dashboard -- this page forces
   its own light theme instead, so every native widget that would
   otherwise pick up the dark-theme defaults (bright lime accents, white
   text meant for a dark background) needs an explicit override here. */
[data-testid="stBaseButton-primary"] {
  background-color:var(--accent) !important; border-color:var(--accent) !important; color:#fff !important;
}
[data-testid="stBaseButton-primary"]:hover {
  background-color:var(--accent-soft) !important; border-color:var(--accent-soft) !important; color:#fff !important;
}
[data-testid="stMetricValue"], [data-testid="stMetricLabel"] { color:var(--ink) !important; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * { color:var(--muted) !important; }
[data-testid="stAlertContentInfo"], [data-testid="stAlertContentSuccess"],
[data-testid="stAlertContentWarning"], [data-testid="stAlertContentError"] { color:var(--ink) !important; }
[data-testid="stExpander"] summary { color:var(--ink) !important; }
[data-testid="stSliderThumbValue"], [data-testid="stTickBarMin"], [data-testid="stTickBarMax"] { color:var(--muted) !important; }
div[data-baseweb="slider"] div[role="slider"] { background-color:var(--accent) !important; }
div[data-testid="stSlider"] div[data-testid="stTickBar"] + div > div { background:var(--accent) !important; }
label[data-baseweb="radio"] [aria-checked="true"] > div:first-child { border-color:var(--accent) !important; }
label[data-baseweb="radio"] [aria-checked="true"] > div:first-child > div { background-color:var(--accent) !important; }
</style>
""", unsafe_allow_html=True)


def _authorized_request() -> bool:
    """Staging entitlement adapter; replace with billing webhook/provider later.

    2026-09-09: full site access opened to everyone (operator decision —
    no paywall while the product is still being validated). To restore
    the token-gated behavior below, set MLB_CUSTOMER_FREE_ACCESS=false
    on the customer-site service — no code change needed either way.
    """
    if os.getenv("MLB_CUSTOMER_FREE_ACCESS", "true").strip().lower() != "false":
        return True
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


def _freshness_label(timestamp: str | None, threshold_seconds: int = 900) -> str:
    """Human-readable age for a timestamp — same "Fresh (Xm ago)" /
    "Stale (Xh ago)" convention the admin dashboard uses for scan data,
    so Arbitrage/Middling cards read the numbers are just as live as the
    EV Picks are. 900s (15 min) default matches how often
    src/arb_middle_scan.py actually reconfirms these opportunities."""
    if not timestamp:
        return "Fresh"
    try:
        seen = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - seen).total_seconds()))
    except (TypeError, ValueError):
        return "Fresh"
    if age > threshold_seconds:
        return f"Stale ({age // 3600}h ago)" if age >= 3600 else f"Stale ({age // 60}m ago)"
    if age < 60:
        return "Fresh (just now)"
    return f"Fresh ({age // 60}m ago)"


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
                WHERE date(scan_timestamp) = ?
                  AND COALESCE(recommendation_tier, 'RESEARCH_ONLY') <> 'OFFICIAL_TRACKED'
                ORDER BY model_score DESC, ev_pct DESC
                LIMIT 25
            """, (get_today_in_configured_timezone(),)).fetchall()
        active_arbitrage = []
        graded_arbitrage = []
        active_middles = []
        graded_middles = []
        if authorized:
            from database.db_manager import (
                get_active_arbitrage_opportunities, get_graded_arbitrage_opportunities,
                get_active_middle_opportunities, get_graded_middle_opportunities,
            )
            active_arbitrage = get_active_arbitrage_opportunities(conn)
            graded_arbitrage = get_graded_arbitrage_opportunities(conn)
            active_middles = get_active_middle_opportunities(conn)
            graded_middles = get_graded_middle_opportunities(conn)

        return {
            "settled": [dict(r) for r in settled],
            "locked": [dict(r) for r in locked],
            "upcoming": [dict(r) for r in upcoming],
            "research": [dict(r) for r in research],
            "active_arbitrage": active_arbitrage,
            "graded_arbitrage": graded_arbitrage,
            "active_middles": active_middles,
            "graded_middles": graded_middles,
        }
    finally:
        conn.close()


def _books_in_opportunities(opportunities: list[dict], book_fields: tuple[str, str]) -> list[str]:
    """Every distinct sportsbook appearing on either leg across a list of
    arbitrage/middle opportunities — the multiselect's option list."""
    field_a, field_b = book_fields
    books = {o.get(field_a) for o in opportunities if o.get(field_a)}
    books |= {o.get(field_b) for o in opportunities if o.get(field_b)}
    return sorted(books)


def _usable_with_books(
    opportunities: list[dict], available_books: set[str], book_fields: tuple[str, str],
) -> list[dict]:
    """Keep only opportunities where BOTH legs are at a book the viewer
    actually selected — an arbitrage or middle isn't usable unless you
    can place both bets, so an opportunity requiring even one book
    outside what you picked doesn't belong in the list."""
    field_a, field_b = book_fields
    return [
        o for o in opportunities
        if o.get(field_a) in available_books and o.get(field_b) in available_books
    ]


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
      <div class="pick-title">{pick.get('player_name') or 'Top Play'}</div>
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
      <div class="pick-meta">{lock.get('event_start_time','')[:16]} · Top Model Play</div>
      <div class="lock-copy">VIP PICK AVAILABLE 🔒</div>
      <div class="pick-meta">Unlock the exact player and wager before first pitch.</div>
    </div>
    """, unsafe_allow_html=True)


def _cumulative_chart(rows: list[dict], label: str) -> None:
    """Small cumulative-units chart shared by the Arbitrage and Middling
    pages -- each track record is kept separate from the main EV Picks
    performance chart and from each other, on purpose."""
    if not rows:
        st.caption(f"No settled {label.lower()} yet.")
        return
    df = pd.DataFrame({
        "Date": [r["graded_at"][:10] for r in rows],
        "Profit": [r["profit_units"] or 0 for r in rows],
    })
    df["Cumulative"] = df["Profit"].cumsum()
    df["Date"] = pd.to_datetime(df["Date"])
    total = df["Cumulative"].iloc[-1]
    color = "#16a34a" if total >= 0 else "#dc2626"
    st.markdown(f"""
    <div class="results-panel">
      <div class="results-eyebrow">{label} Track Record</div>
      <div class="results-number" style="color:{color};">{total:+.2f}u</div>
      <div class="results-caption">Cumulative result of every settled {label.lower()} opportunity, in order.</div>
    </div>
    """, unsafe_allow_html=True)
    chart = alt.Chart(df).mark_area(
        line={"color": color, "strokeWidth": 2.5}, color=color, opacity=0.16, interpolate="monotone",
    ).encode(
        x=alt.X("Date:T", title=None), y=alt.Y("Cumulative:Q", title="Cumulative units"),
    )
    st.altair_chart(chart, use_container_width=True)


def _render_arbitrage_card(opp: dict) -> None:
    pick_label = f"{_market_label(opp['market_type'])}" + (
        f" {opp['line']}" if opp.get("line") is not None else ""
    )
    fresh = _freshness_label(opp.get("last_seen_at"))
    game_time = format_event_start_local(opp.get("event_start_time"))
    st.markdown(f"""
    <div class="pick">
      <div class="pick-title">{opp.get('player_name') or opp.get('matchup') or pick_label}</div>
      <div class="pick-meta">{opp.get('matchup', '')} · {pick_label} · <span class="edge">{fresh}</span></div>
      <div class="pick-meta">Game starts: {game_time}</div>
      <div class="pick-meta">{opp['side_a']} · {opp['side_a_sportsbook']} {opp['side_a_price']:+d}
        ({opp['side_a_stake_pct']:.0%} stake)</div>
      <div class="pick-meta">{opp['side_b']} · {opp['side_b_sportsbook']} {opp['side_b_price']:+d}
        ({opp['side_b_stake_pct']:.0%} stake)</div>
      <div class="unit-line">Guaranteed: <span class="result-win">+{opp['guaranteed_roi_pct']:.2f}%</span></div>
    </div>
    """, unsafe_allow_html=True)


def _render_middle_card(opp: dict) -> None:
    fresh = _freshness_label(opp.get("last_seen_at"))
    game_time = format_event_start_local(opp.get("event_start_time"))
    st.markdown(f"""
    <div class="pick">
      <div class="pick-title">{opp.get('player_name') or opp.get('matchup') or _market_label(opp['market_type'])}</div>
      <div class="pick-meta">{opp.get('matchup', '')} · {_market_label(opp['market_type'])} · <span class="edge">{fresh}</span></div>
      <div class="pick-meta">Game starts: {game_time}</div>
      <div class="pick-meta">Over {opp['over_line']} · {opp['over_sportsbook']} {opp['over_price']:+d}</div>
      <div class="pick-meta">Under {opp['under_line']} · {opp['under_sportsbook']} {opp['under_price']:+d}</div>
      <div class="unit-line">Worst case: <span class="result-loss">{opp['worst_case_roi_pct']:+.2f}%</span>
        · Best case: <span class="result-win">+{opp['best_case_roi_pct']:.2f}%</span></div>
    </div>
    """, unsafe_allow_html=True)


authorized = _authorized_request()
try:
    data = load_customer_data(authorized)
except Exception:
    logger.exception("Customer data load failed")
    st.error("The model data is temporarily unavailable. Please check back shortly.")
    st.stop()

if "view_mode" not in st.session_state:
    st.session_state.view_mode = None

today = datetime.now(timezone.utc).strftime("%B %d, %Y")
st.markdown(f"""
<div class="topnav">
  <div class="topnav-brand">
    <span class="topnav-mark">VIP</span>
    <span class="topnav-word">Sharp Market Intelligence</span>
  </div>
</div>
<div class="hero">
  <div class="eyebrow">MLB · NFL · WNBA</div>
  <h1>Multi-book odds analysis</h1>
  <p>Every price is screened against fair value and market quality before it's shown. Every result — win or loss — is tracked and published in full.</p>
  <span class="pill">{today} · {'FULL ACCESS' if authorized else 'PUBLIC VIEW'}</span>
</div>
""", unsafe_allow_html=True)

if st.session_state.view_mode is not None:
    if st.button("← All Options", key="back_to_menu"):
        st.session_state.view_mode = None
        st.rerun()
    st.divider()

# ==================================================================
# Menu — three boxes, pick a mode
# ==================================================================
if st.session_state.view_mode is None:
    st.subheader("What do you want to see?")
    st.caption("Three independent ways to use this model. Pick one.")
    menu_cols = st.columns(3)

    with menu_cols[0]:
        with st.container(border=True):
            st.markdown("### 📈 EV Picks")
            st.markdown(
                "The model's own top-scored plays, checked against Pinnacle and the "
                "wider market. A verified, nothing-hidden track record."
            )
            st.metric("Live today", len(data["upcoming"]) if authorized else len(data["locked"]))
            if st.button("View EV Picks →", key="choose_ev", use_container_width=True, type="primary"):
                st.session_state.view_mode = "ev"
                st.rerun()

    with menu_cols[1]:
        with st.container(border=True):
            st.markdown("### ⚖️ Arbitrage")
            st.markdown(
                "Cross-book price mismatches. Bet both sides yourself — a guaranteed "
                "profit no matter which side wins."
            )
            st.metric("Live now", len(data["active_arbitrage"]))
            if st.button("View Arbitrage →", key="choose_arb", use_container_width=True, type="primary"):
                st.session_state.view_mode = "arbitrage"
                st.rerun()

    with menu_cols[2]:
        with st.container(border=True):
            st.markdown("### ⚡ Middling")
            st.markdown(
                "Over at one line, Under at a higher line, two different books. Small "
                "capped risk, big upside if the number lands in between."
            )
            st.metric("Live now", len(data["active_middles"]))
            if st.button("View Middling →", key="choose_mid", use_container_width=True, type="primary"):
                st.session_state.view_mode = "middling"
                st.rerun()

# ==================================================================
# EV Picks
# ==================================================================
elif st.session_state.view_mode == "ev":
    if not authorized:
        st.info("Top plays are posted when the slate qualifies. Subscriber access unlocks the exact wager before the game; settled picks become public automatically for full accountability.")
        st.subheader("Today's Top Picks")
        if data["locked"]:
            for lock in data["locked"]:
                _render_locked_pick(lock)
            st.button("Unlock Today's Picks", type="primary", use_container_width=True, disabled=True)
        else:
            st.success("No Top Picks Yet")
            st.caption("The model has not identified an opportunity meeting today's qualification standards.")
    else:
        st.subheader("Today's Top Picks — Upcoming")
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
            st.success("No Top Picks Yet")
            st.caption("The model has not identified an opportunity meeting today's qualification standards.")
        if data["research"]:
            with st.expander("Full Board"):
                for pick in data["research"]:
                    _render_full_pick(pick)

    st.divider()
    st.subheader("Verified Track Record — Past Picks")
    st.caption("Settled Top Picks only. Winners and losses are included equally; no results are manually selected or hidden.")
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
            line_color = "#16a34a" if positive else "#dc2626"

            st.markdown(f"""
            <div class="results-panel">
              <div class="results-eyebrow">Real Results — {period}</div>
              <div class="results-number" style="color:{line_color};">{period_units:+.2f}u</div>
              <div class="results-caption">Cumulative result if every Top Pick were followed at its recorded stake.
              Every settled pick counts — wins and losses included equally, nothing hidden or cherry-picked.</div>
            </div>
            """, unsafe_allow_html=True)

            area = alt.Chart(chart_df).mark_area(
                line={"color": line_color, "strokeWidth": 2.5},
                color=line_color, opacity=0.16, interpolate="monotone",
            ).encode(
                x=alt.X("Date:T", title=None,
                        axis=alt.Axis(grid=False, labelColor="#6b7280", tickColor="#e5e7eb", domainColor="#e5e7eb")),
                y=alt.Y("Actual Units:Q", title="Cumulative units",
                        axis=alt.Axis(grid=True, gridColor="#f0f1f3", labelColor="#6b7280", titleColor="#6b7280")),
                tooltip=[alt.Tooltip("Date:T", title="Date"), alt.Tooltip("Actual Units:Q", format="+.2f")],
            )
            expected_line = alt.Chart(chart_df).mark_line(
                color="#9ca3af", strokeDash=[4, 3], strokeWidth=1.6, interpolate="monotone", opacity=0.85,
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
        st.info("BUILDING VERIFIED TRACK RECORD · Performance appears after Top Picks settle.")

# ==================================================================
# Arbitrage
# ==================================================================
elif st.session_state.view_mode == "arbitrage":
    st.subheader("⚖️ Arbitrage — guaranteed profit")
    st.caption(
        "Cross-book price mismatches — bet both sides yourself, at the two books shown. "
        "The combined stake guarantees a profit no matter which side wins. Not a \"pick\" "
        "to follow; you place both bets."
    )
    if not authorized:
        st.info("Subscriber access unlocks live arbitrage opportunities.")
    else:
        if not data["active_arbitrage"]:
            st.success("No arbitrage opportunities right now.")
            st.caption("Rechecked every ~15 minutes as odds move.")
        else:
            arb_book_fields = ("side_a_sportsbook", "side_b_sportsbook")
            arb_all_books = _books_in_opportunities(data["active_arbitrage"], arb_book_fields)
            arb_selected_books = render_sportsbook_picker(arb_all_books, key_prefix="cust_arb")
            arb_usable = _usable_with_books(data["active_arbitrage"], arb_selected_books, arb_book_fields)
            if not arb_usable:
                st.warning("No arbitrage opportunities usable with the sportsbooks selected above.")
            else:
                if len(arb_usable) < len(data["active_arbitrage"]):
                    st.caption(f"{len(arb_usable)} of {len(data['active_arbitrage'])} opportunities usable with your selected books.")
                arb_cols = st.columns(2)
                for i, opp in enumerate(arb_usable):
                    with arb_cols[i % 2]:
                        _render_arbitrage_card(opp)
        st.divider()
        _cumulative_chart(data["graded_arbitrage"], "Arbitrage")

# ==================================================================
# Middling
# ==================================================================
elif st.session_state.view_mode == "middling":
    st.subheader("⚡ Middling — small guaranteed risk, big upside")
    st.caption(
        "Over at a lower line, Under at a higher line, two different books. If the final "
        "number lands in the window, both bets win. Outside it, the guaranteed worst case "
        "is capped small — never a full loss on both legs."
    )
    if not authorized:
        st.info("Subscriber access unlocks live middling opportunities.")
    else:
        if not data["active_middles"]:
            st.success("No middle opportunities right now.")
            st.caption("Rechecked every ~15 minutes as odds move.")
        else:
            mid_book_fields = ("over_sportsbook", "under_sportsbook")
            mid_all_books = _books_in_opportunities(data["active_middles"], mid_book_fields)
            mid_selected_books = render_sportsbook_picker(mid_all_books, key_prefix="cust_mid")
            mid_usable = _usable_with_books(data["active_middles"], mid_selected_books, mid_book_fields)
            if not mid_usable:
                st.warning("No middle opportunities usable with the sportsbooks selected above.")
            else:
                if len(mid_usable) < len(data["active_middles"]):
                    st.caption(f"{len(mid_usable)} of {len(data['active_middles'])} opportunities usable with your selected books.")
                mid_cols = st.columns(2)
                for i, opp in enumerate(mid_usable):
                    with mid_cols[i % 2]:
                        _render_middle_card(opp)
        st.divider()
        _cumulative_chart(data["graded_middles"], "Middling")

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

st.caption("This platform does not guarantee profit, place bets, or present Full Board signals as Top Picks.")
