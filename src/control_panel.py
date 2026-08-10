"""Local one-click control panel for the MLB VIP Model.

Launch with:
    python -m streamlit run src/control_panel.py
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from database.connection import get_database_url
from database.db_manager import get_connection
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

# ── Path setup ─────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# ── Page config (must be first Streamlit call) ─────────────────────
st.set_page_config(
    page_title="MLB VIP Model",
    page_icon="⚾",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Signature theme: "Sharp Market Intelligence" ─────────────────────
# Near-black navy with a soft radial glow, vivid lime for positive EV,
# cyan secondary, amber leans, red risk. Panels read like glass cards.
st.markdown(
    """
    <style>
    /* Force the near-black navy background even if the host applies a
       light base theme (e.g. Render CLI flags). */
    .stApp {
        background: #080B12;
    }
    [data-testid="stHeader"] {
        background: transparent;
    }
    [data-testid="stSidebar"] {
        background: #0B1120;
    }
    [data-testid="stSidebar"] * {
        color: #F6F8FC;
    }
    [data-testid="stAppViewContainer"] {
        background: radial-gradient(1000px 500px at 70% -10%, #1B2440 0%, transparent 60%), #080B12;
        color: #F6F8FC;
    }
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li {
        color: #F6F8FC;
    }

    /* Metric / KPI cards read like glass panels with a lime value. */
    [data-testid="stMetric"] {
        background: linear-gradient(145deg, rgba(23, 31, 46, .95), rgba(13, 18, 28, .95));
        border: 1px solid rgba(255, 255, 255, .08);
        border-radius: 14px;
        padding: 16px 18px;
    }
    [data-testid="stMetricValue"] {
        color: #F6F8FC;
        font-family: 'JetBrains Mono', monospace;
        font-weight: 800;
        letter-spacing: -0.03em;
        font-variant-numeric: tabular-nums;
    }
    [data-testid="stMetricLabel"] {
        color: #8E9AAE;
        text-transform: uppercase;
        letter-spacing: 0.09em;
        font-size: 0.72rem;
        font-weight: 800;
        opacity: 0.9;
    }

    /* Bordered containers become hairline glass cards. */
    [data-testid="stVerticalBlockBorderWrapper"] > div:has(> [data-testid="stVerticalBlock"]) {
        border-radius: 14px;
        border-color: rgba(255, 255, 255, .08);
        transition: border-color 0.15s ease;
    }

    /* Section subheaders: bold, tight */
    h3 {
        font-weight: 800 !important;
        letter-spacing: -0.02em;
    }

    /* Tabs: bold, uppercase, lime underline on the active tab */
    button[data-baseweb="tab"] {
        font-weight: 750;
        text-transform: uppercase;
        font-size: 0.78rem;
        letter-spacing: 0.04em;
        color: #8E9AAE;
    }
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #B9FF45 !important;
    }
    div[data-baseweb="tab-highlight"] {
        background-color: #B9FF45 !important;
        height: 3px !important;
    }

    /* Dataframes: monospace numerals throughout for that odds-board feel */
    [data-testid="stDataFrame"] {
        font-family: 'JetBrains Mono', monospace;
        font-variant-numeric: tabular-nums;
    }

    /* Badge pills: rounded, uppercase, letter-spaced */
    span[style*="background-color"] {
        border-radius: 999px !important;
        padding: 4px 10px !important;
        font-weight: 850 !important;
        font-size: 0.7rem !important;
        letter-spacing: 0.06em !important;
        text-transform: uppercase !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Lazy imports (after page config) ───────────────────────────────

def _import_config():
    from src.production_config import load_config
    return load_config()

def _import_shadow():
    from src.shadow_mode import load_shadow_config
    return load_shadow_config()

def _import_health():
    from src.health_check import run_health_checks
    return run_health_checks

def _import_dashboard():
    from src.shadow_dashboard import build_dashboard
    return build_dashboard

def _import_backup():
    from src.backup_database import backup_database, list_backups
    return backup_database, list_backups

def _import_db_manager():
    from database.db_manager import get_connection, init_db
    return get_connection, init_db


def _theme_chart(chart, height: int = 320):
    """Apply the Sharp Market Intelligence palette to an Altair chart."""
    import altair as alt

    return (
        chart.configure(
            background="transparent",
            padding={"left": 8, "right": 8, "top": 8, "bottom": 4},
            view={"stroke": "transparent"},
            axis={
                "labelColor": "#8E9AAE",
                "tickColor": "transparent",
                "gridColor": "rgba(255,255,255,0.06)",
                "titleColor": "#8E9AAE",
                "domainColor": "rgba(255,255,255,0.14)",
                "titleFontWeight": 700,
                "labelFontSize": 11,
                "titleFontSize": 12,
            },
            legend={"labelColor": "#8E9AAE", "titleColor": "#8E9AAE", "orient": "top"},
        )
        .properties(height=height)
    )


# ==================================================================
# Helpers
# ==================================================================

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _get_config():
    try:
        return _import_config()
    except Exception:
        return None


def _get_shadow():
    try:
        return _import_shadow()
    except Exception:
        return None


def _redact(value: str, show: int = 4) -> str:
    if not value or len(value) <= show:
        return "****"
    return value[:show] + "*" * min(8, len(value) - show)


def _status_color(status: str) -> str:
    return {
        "ok": "green", "healthy": "green", "pass": "green", "success": "green",
        "warning": "orange", "degraded": "orange",
        "error": "red", "unhealthy": "red", "fail": "red", "failed": "red",
    }.get(status, "gray")


def _format_market_type(mt: str) -> str:
    return mt.replace("_", " ").title() if mt else ""


def _format_pick_side_line(rec: dict) -> str:
    """Render O/U lines and Y/N conditions without a misleading None."""
    side = (rec.get("side") or "").title()
    line = rec.get("line")
    if line is not None:
        return f"{side} {line}"
    labels = {
        "batting_hits_yn": "1+ hit",
        "batting_homeRuns_yn": "1+ home run",
        "batting_stolenBases_yn": "1+ stolen base",
        "pitching_strikeouts_yn": "1+ strikeout",
        "pitching_earnedRuns_yn": "1+ earned run",
        "pitching_win_yn": "pitcher win",
    }
    return f"{side} · {labels.get(rec.get('market_type'), 'binary result')}"


# ── Shared "betting-slip stub" card renderer ────────────────────────
# Left-edge color encodes tier/outcome; the headline number's color
# encodes direction (lime = positive/win, red = negative/loss).
_TIER_STUB = {
    "OFFICIAL_TRACKED": ("#B9FF45", "VIP OFFICIAL"),
    "DISCOVERY_TRACKED": ("#A995FF", "DISCOVERY"),
}
_OUTCOME_STUB = {
    "win": ("#B9FF45", "WIN"),
    "loss": ("#FF3D58", "LOSS"),
    "push": ("#8E9AAE", "PUSH"),
    "pending": ("#FFC75F", "PENDING"),
}


def _render_pick_stub_card(
    rank: str,
    stub_hex: str,
    stub_label: str,
    title: str,
    subtitle: str,
    detail: str,
    headline: str,
    headline_hex: str,
    tail: str = "",
) -> None:
    """Render one betting-slip-stub card. Caller supplies pre-formatted strings."""
    st.markdown(
        f"""
        <div style="
            background: linear-gradient(145deg, rgba(23, 31, 46, .95), rgba(13, 18, 28, .95));
            border: 1px solid rgba(255, 255, 255, .08);
            border-left: 4px solid {stub_hex};
            border-radius: 14px;
            padding: 14px 16px;
            margin-bottom: 8px;
        ">
            <div style="
                font-family: 'JetBrains Mono', monospace;
                font-size: 0.68rem;
                font-weight: 700;
                letter-spacing: 0.06em;
                color: {stub_hex};
            ">{rank} · {stub_label}</div>
            <div style="
                font-weight: 800;
                font-size: 1.02rem;
                margin-top: 4px;
                color: #F6F8FC;
            ">{title}</div>
            <div style="color:#8E9AAE; font-size:0.82rem; margin-top:2px;">{subtitle}</div>
            <div style="
                font-family: 'JetBrains Mono', monospace;
                color:#8E9AAE;
                font-size:0.8rem;
                margin-top:6px;
            ">{detail}</div>
            <div style="
                font-family: 'JetBrains Mono', monospace;
                font-weight:700;
                font-size:0.95rem;
                color:{headline_hex};
                margin-top:8px;
            ">{headline} <span style="color:#8E9AAE; font-weight:500; font-size:0.78rem;">{tail}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _is_postgres() -> bool:
    """Check whether PostgreSQL is configured (vs local SQLite)."""
    return bool(get_database_url())


def _should_query(db_path: str) -> bool:
    """Return True if a database query can proceed.

    When PostgreSQL is configured, always query (the file check is irrelevant).
    Otherwise, verify the SQLite file exists.
    """
    return _is_postgres() or Path(db_path).exists()


def _open_dashboard_connection(db_path: str):
    """Open the configured production database, or the local test database."""
    url = get_database_url()
    if url:
        return get_connection()
    return get_connection(db_path=str(db_path))


def _load_todays_recs(db_path: str) -> list[dict[str, Any]]:
    """Load today's frozen recommendations from the database."""
    return _load_recs(db_path, "today")


def _load_recs(db_path: str, filter_mode: str = "latest") -> list[dict[str, Any]]:
    """Load recommendations with flexible filtering.

    filter_mode:
        "latest" — only the most recent scan_run_id
        "today"  — all recs with today's scan_timestamp
        "all"    — all recs in the database
    """
    if not _should_query(db_path):
        return []
    try:
        conn = _open_dashboard_connection(db_path)
        try:
            cols = (
                "recommendation_id, event_id, player_name, market_type, "
                "market_form, period, line, side, sportsbook, "
                "offered_american_odds, offered_decimal_odds, "
                "ev_pct, yn_implied_prob_adv, yn_reference_prob, "
                "rec_status, observation_timestamp, scan_timestamp, "
                "freshness_status, fingerprint, scan_run_id, "
                "matchup, event_status, event_start_time, "
                "model_score, score_version, score_explanation, "
                "recommendation_tier, qualification_passed, "
                "qualification_reasons, disqualification_reasons, "
                "n_consensus_books, market_quality, "
                "points_to_7, price_outlier_capped, true_ev_unavailable, "
                "one_sided_market, insufficient_books_failure, "
                "market_quality_score, score_components"
            )

            def _run_query(where_clause: str, params: tuple = ()) -> list:
                for col_list in (cols, "*"):
                    try:
                        return conn.execute(
                            f"SELECT {col_list} FROM historical_recommendations {where_clause}",
                            params,
                        ).fetchall()
                    except Exception:
                        continue
                return []

            if filter_mode == "latest":
                rows = _run_query(
                    "WHERE scan_run_id = ("
                    "  SELECT run_id FROM scan_runs "
                    "  WHERE run_type = 'scan' AND finished_at IS NOT NULL "
                    "  ORDER BY started_at DESC LIMIT 1"
                    ") ORDER BY ev_pct DESC"
                )
            elif filter_mode == "today":
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                rows = _run_query(
                    "WHERE scan_timestamp LIKE ? ORDER BY ev_pct DESC",
                    (f"{today}%",),
                )
            else:  # "all"
                rows = _run_query("ORDER BY scan_timestamp DESC, ev_pct DESC")
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _get_latest_run_id(db_path: str) -> str:
    """Get the most recent scan run_id from scan_runs."""
    if not _should_query(db_path):
        return ""
    try:
        conn = _open_dashboard_connection(db_path)
        row = conn.execute(
            "SELECT run_id FROM scan_runs "
            "WHERE run_type = 'scan' AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return row["run_id"] if row else ""
    except Exception:
        return ""


def _get_data_freshness(db_path: str, threshold_seconds: int = 3600) -> str:
    """Return a human-readable age for the latest completed scan run."""
    if not _should_query(db_path):
        return "No data"
    try:
        conn = _open_dashboard_connection(db_path)
        row = conn.execute(
            "SELECT finished_at FROM scan_runs "
            "WHERE run_type = 'scan' AND finished_at IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row or not row["finished_at"]:
            return "No data"
        finished = row["finished_at"]
        if isinstance(finished, str):
            finished = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - finished).total_seconds()))
        if age > threshold_seconds:
            return f"Stale ({age // 3600}h ago)"
        return f"Fresh ({age // 60}m ago)"
    except Exception:
        return "No data"


def _get_schedule_summary(db_path: str, run_summary: dict | None = None) -> dict[str, Any]:
    """Get today's game schedule summary from the games table.

    Validates that Total = Upcoming + Live + Completed + Postponed + Cancelled,
    and that Analyzed + Skipped = Eligible (total minus postponed/cancelled).

    Analyzed and Skipped are always counted by unique event_id from the
    latest completed pipeline run, never by recommendation/prop rows.
    Skipped is derived as eligible - analyzed so the invariant always holds.
    """
    result: dict[str, Any] = {
        "total": 0, "upcoming": 0, "live": 0, "completed": 0,
        "postponed": 0, "cancelled": 0,
        "analyzed": 0, "skipped": 0, "recommendations": 0,
        "eligible": 0, "valid": True,
    }
    if not _should_query(db_path):
        return result
    try:
        conn = _open_dashboard_connection(db_path)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM games WHERE date(start_time) = ?", (today,)
        ).fetchone()["total"]
        result["total"] = total

        for status_val, key in [
            ("scheduled", "upcoming"),
            ("live", "live"),
            ("final", "completed"),
            ("postponed", "postponed"),
            ("cancelled", "cancelled"),
        ]:
            result[key] = conn.execute(
                "SELECT COUNT(*) AS cnt FROM games WHERE date(start_time) = ? AND status = ?",
                (today, status_val),
            ).fetchone()["cnt"]

        result["eligible"] = max(
            0, total - result["postponed"] - result["cancelled"]
        )

        latest_run = conn.execute(
            "SELECT run_id FROM scan_runs "
            "WHERE run_type = 'scan' AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if latest_run:
            run_id = latest_run["run_id"]
            result["recommendations"] = conn.execute(
                "SELECT COUNT(*) AS cnt FROM historical_recommendations WHERE scan_run_id = ?",
                (run_id,),
            ).fetchone()["cnt"]
            result["analyzed"] = conn.execute(
                "SELECT COUNT(DISTINCT event_id) AS cnt FROM historical_recommendations WHERE scan_run_id = ?",
                (run_id,),
            ).fetchone()["cnt"]

        result["skipped"] = max(0, result["eligible"] - result["analyzed"])
        result["valid"] = True

        conn.close()
    except Exception:
        pass
    return result


def _get_deduplicated_skipped_games(run_summary: dict | None) -> list[dict]:
    """Return skipped games from the latest run, deduplicated by event_id."""
    if not run_summary:
        return []
    skipped = run_summary.get("skipped_games") or []
    seen: set[str] = set()
    deduped: list[dict] = []
    for sg in skipped:
        eid = sg.get("event_id", "")
        if eid and eid in seen:
            continue
        if eid:
            seen.add(eid)
        deduped.append(sg)
    return deduped


def _get_live_game_warnings(db_path: str, run_id: str) -> list[dict]:
    """Check if any recommendations in the given run belong to live/completed games."""
    warnings: list[dict] = []
    if not _should_query(db_path) or not run_id:
        return warnings
    try:
        conn = _open_dashboard_connection(db_path)
        rows = conn.execute(
            "SELECT recommendation_id, matchup, event_status, player_name, "
            "market_type, sportsbook FROM historical_recommendations "
            "WHERE scan_run_id = ? AND event_status IN "
            "('live','inprogress','in_progress','started','in-progress',"
            "'final','finished','completed','closed','ended')",
            (run_id,),
        ).fetchall()
        conn.close()
        warnings = [dict(r) for r in rows]
    except Exception:
        pass
    return warnings


def _load_latest_run_summary(output_dir: str) -> dict | None:
    """Load the most recent run_summary.json from output_dir."""
    p = Path(output_dir) / "run_summary.json"
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_latest_report(output_dir: str) -> str | None:
    """Find the most recent report file."""
    out = Path(output_dir)
    if not out.exists():
        return None
    reports = sorted(out.glob("recommendations.*"), reverse=True)
    if reports:
        return str(reports[0])
    return None


def _get_backup_dir(config) -> Path:
    return Path(config.output_dir) / "backups"


# ==================================================================
# Session state defaults
# ==================================================================

if "run_active" not in st.session_state:
    st.session_state.run_active = False
if "run_log" not in st.session_state:
    st.session_state.run_log = []
if "run_result" not in st.session_state:
    st.session_state.run_result = None
if "last_run_time" not in st.session_state:
    st.session_state.last_run_time = None
if "last_run_steps" not in st.session_state:
    st.session_state.last_run_steps = []


# ==================================================================
# Pipeline runner (background thread)
# ==================================================================

def _run_pipeline_background(output_dir: str, result_container: dict) -> None:
    """Run the pipeline in a background thread, capturing output."""
    try:
        result_container["status"] = "running"
        result_container["steps"] = []
        result_container["exit_code"] = None
        result_container["output"] = ""
        result_container["error"] = ""

        cmd = [sys.executable, "-m", "src.daily_pipeline", "--output-dir", output_dir]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(_ROOT),
        )
        result_container["exit_code"] = proc.returncode
        result_container["output"] = proc.stdout + proc.stderr
        result_container["status"] = "success" if proc.returncode in (0, 1) else "failed"
    except subprocess.TimeoutExpired:
        result_container["status"] = "failed"
        result_container["error"] = "Pipeline timed out after 10 minutes"
        result_container["exit_code"] = -1
    except Exception as exc:
        result_container["status"] = "failed"
        result_container["error"] = str(exc)
        result_container["exit_code"] = -1


def _run_subprocess_command(label: str, cmd: list[str], result_container: dict, timeout: int = 120) -> None:
    """Run a subprocess command and capture output."""
    try:
        result_container["status"] = "running"
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(_ROOT)
        )
        result_container["exit_code"] = proc.returncode
        result_container["output"] = proc.stdout + proc.stderr
        result_container["status"] = "success" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        result_container["status"] = "failed"
        result_container["error"] = f"{label} timed out"
        result_container["exit_code"] = -1
    except Exception as exc:
        result_container["status"] = "failed"
        result_container["error"] = str(exc)
        result_container["exit_code"] = -1


# ==================================================================
# Main layout
# ==================================================================

config = _get_config()
shadow = _get_shadow()
db_path = config.database_path if config else os.environ.get("MLB_DB_PATH", "")
output_dir_val = config.output_dir if config else "output"

# Initialize the complete schema before any dashboard query runs. This is
# idempotent and uses DATABASE_URL through db_manager in production.
if not st.session_state.get("_schema_ensured"):
    try:
        from database.db_manager import init_db, ensure_official_picks_schema
        init_db(db_path)
        ensure_official_picks_schema()
        st.session_state["_schema_ensured"] = True
    except Exception as exc:
        st.error(f"Database schema initialization failed: {exc}")
        raise RuntimeError("Dashboard database schema initialization failed") from exc

# Populate last_run_time from the most recent completed scan run
if st.session_state.last_run_time is None:
    try:
        _conn = _open_dashboard_connection(db_path)
        _row = _conn.execute(
            "SELECT COALESCE(finished_at, started_at) AS ts FROM scan_runs "
            "WHERE run_type = 'scan' AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        _conn.close()
        if _row and _row["ts"]:
            st.session_state.last_run_time = _row["ts"]
    except Exception:
        pass

# ── Top bar ────────────────────────────────────────────────────────
is_shadow = bool(shadow and shadow.shadow_mode)
_pill_bg = "rgba(255,199,95,.14)" if is_shadow else "rgba(185,255,69,.08)"
_pill_col = "#FFC75F" if is_shadow else "#B9FF45"
_pill_txt = "SHADOW MODE" if is_shadow else "MODEL LIVE"
st.markdown(
    f"""
    <div style="display:flex;align-items:center;justify-content:space-between;gap:18px;
        border-bottom:1px solid rgba(255,255,255,.08);padding:6px 0 14px;">
        <div style="display:flex;align-items:center;gap:11px;font-weight:800;letter-spacing:.02em;">
            <span style="display:grid;place-items:center;width:36px;height:36px;border-radius:10px;
                background:linear-gradient(135deg,#B9FF45,#48D8FF);color:#0A1017;font-size:19px;">⚾</span>
            <span style="line-height:1.05;">
                <span style="display:block;font-size:15px;">MLB VIP MODEL</span>
                <small style="display:block;color:#8E9AAE;font-size:10px;letter-spacing:.12em;font-weight:700;">SHARP MARKET INTELLIGENCE</small>
            </span>
        </div>
        <div style="border:1px solid {_pill_col}40;background:{_pill_bg};color:{_pill_col};
            padding:7px 12px;border-radius:999px;font-size:12px;font-weight:800;">
            <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{_pill_col};
                box-shadow:0 0 10px {_pill_col};margin-right:7px;"></span>{_pill_txt}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Hero ───────────────────────────────────────────────────────────
st.markdown(
    f"""
    <div style="padding:40px 0 24px;display:flex;align-items:end;justify-content:space-between;gap:25px;flex-wrap:wrap;">
        <div>
            <div style="color:#B9FF45;font-weight:800;font-size:12px;letter-spacing:.13em;text-transform:uppercase;">
                {datetime.now(timezone.utc).strftime('%B %d, %Y')} · MLB Slate
            </div>
            <h1 style="margin:8px 0 10px;font-size:clamp(30px,4.5vw,54px);line-height:1;letter-spacing:-.055em;">Find the price. <em style="color:#B9FF45;font-style:normal;">Beat the market.</em></h1>
            <p style="margin:0;color:#8E9AAE;max-width:570px;font-size:16px;">Pinnacle-first player props, checked against the market and delivered with complete transparency.</p>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

meta_cols = st.columns(5, border=True)
with meta_cols[0]:
    st.metric("DATE", datetime.now(timezone.utc).strftime("%a %b %d, %Y"))
with meta_cols[1]:
    st.metric("TIMEZONE", config.timezone if config else "America/New_York")
with meta_cols[2]:
    st.metric("LAST RUN", st.session_state.last_run_time or "Not yet run")
with meta_cols[3]:
    st.metric("DATA FRESHNESS", _get_data_freshness(db_path, config.freshness_threshold_seconds if config else 3600))
with meta_cols[4]:
    st.metric("DATABASE", "PostgreSQL" if _is_postgres() else Path(db_path).name)

# ── Tab Layout ─────────────────────────────────────────────────────
tabs = st.tabs([
    ":material/star: Today's Picks",
    ":material/verified: Official Picks",
    ":material/science: Research",
    ":material/trending_up: Line Movement",
    ":material/analytics: Performance",
    ":material/insights: Market Intelligence",
    ":material/settings: Run & Operations",
    ":material/psychology: Adaptive Learning",
])

# ==================================================================
# Tab 1: Today's Picks
# ==================================================================
with tabs[0]:
    st.subheader(":material/star: Today's Picks")
    st.caption("The model's live record and picks, newest first. Results update as games settle.")

    import pandas as pd
    from src.tracker import (
        get_official_picks,
        compute_performance,
        compute_variable_stake,
        WIN,
        LOSS,
        PUSH,
    )

    conn_tab1 = _open_dashboard_connection(db_path)
    try:
        picks_board = get_official_picks(
            conn_tab1, tier="OFFICIAL_TRACKED", today_only=True,
        )
        board_metrics = compute_performance(conn_tab1, today_only=True)
    finally:
        conn_tab1.close()

    def _result_badge(outcome: str | None) -> str:
        o = (outcome or "pending").lower()
        if o == WIN:
            return ":green-background[**W**]"
        if o == LOSS:
            return ":red-background[**L**]"
        if o == PUSH:
            return ":gray-background[**P**]"
        return ":blue-background[Pending]"

    def _current_streak(picks: list[dict]) -> str:
        streak = 0
        ordered = sorted(picks, key=lambda p: p.get("selected_at") or "")
        for p in ordered:
            o = (p.get("outcome") or "pending").lower()
            if o == WIN:
                streak = streak + 1 if streak >= 0 else 1
            elif o == LOSS:
                streak = streak - 1 if streak <= 0 else -1
        if streak > 0:
            return f"{streak}W"
        if streak < 0:
            return f"{-streak}L"
        return "-"

    # ── Record header (scoreboard-style) ──
    rec_cols = st.columns(6, border=True)
    rec_cols[0].metric("Wins", board_metrics.wins)
    rec_cols[1].metric("Losses", board_metrics.losses)
    rec_cols[2].metric("Pushes", board_metrics.pushes)
    rec_cols[3].metric(
        "Win Rate",
        f"{board_metrics.win_rate:.1%}" if board_metrics.graded else "-",
    )
    rec_cols[4].metric("Units", f"{board_metrics.units_won:+.2f}")
    rec_cols[5].metric("Streak", _current_streak(picks_board))

    if picks_board:
        # ── Top Picks strip (betting-slip stubs) ──
        top = sorted(picks_board, key=lambda p: -(p.get("ev_pct") or 0.0))[:4]
        st.markdown("#### :material/grade: Top Picks")
        top_cols = st.columns(4)
        for i, r in enumerate(top):
            is_yn = (r.get("market_form") or "").lower() == "yn" or str(r.get("market_type", "")).endswith("_yn")
            edge = (r.get("yn_implied_prob_adv") if is_yn else r.get("ev_pct")) or 0.0
            edge_hex = "#17C964" if edge > 0 else ("#F31260" if edge < 0 else "#7A8CAB")
            tier = r.get("tier") or r.get("recommendation_tier")
            stub_hex, stub_label = _TIER_STUB.get(tier, ("#F5A623", "VIP OFFICIAL"))
            with top_cols[i]:
                _render_pick_stub_card(
                    rank=f"#{i + 1}",
                    stub_hex=stub_hex,
                    stub_label=stub_label,
                    title=r.get("player_name", ""),
                    subtitle=f"{_format_market_type(r.get('market_type', ''))} · {_format_pick_side_line(r)}",
                    detail=f"{r.get('sportsbook', '')} @ {r.get('offered_american_odds', '')}",
                    headline=f"{edge:+.2f}{' pp' if is_yn else '%'}",
                    headline_hex=edge_hex,
                    tail=f"{'price advantage' if is_yn else 'edge'} · score {round(r.get('model_score') or 0, 1)}",
                )

    st.divider()

    if not picks_board:
        st.info("No picks on the board yet. Run the pipeline from the Run & Operations tab.")
    else:
        board_rows = []
        for p in picks_board:
            player = p.get("player_name", "")
            market = _format_market_type(p.get("market_type", ""))
            pick_line = _format_pick_side_line(p)
            pick_label = " · ".join(x for x in [player, market, pick_line] if x)
            units = p.get("risk_units")
            if units is None:
                units = compute_variable_stake(
                    p.get("ev_pct"), p.get("offered_decimal_odds"), p.get("model_score"),
                )
            board_rows.append({
                "Date": (p.get("selected_at") or p.get("event_start_time") or "")[:16],
                "Matchup": p.get("matchup", ""),
                "Pick": pick_label,
                "Odds": p.get("offered_american_odds"),
                "Units": round(units, 2) if units is not None else None,
                "Result": _result_badge(p.get("outcome")),
            })

        st.dataframe(
            pd.DataFrame(board_rows),
            column_config={
                "Date": st.column_config.TextColumn("Date"),
                "Matchup": st.column_config.TextColumn("Matchup"),
                "Pick": st.column_config.TextColumn("Pick"),
                "Odds": st.column_config.TextColumn("Odds"),
                "Units": st.column_config.NumberColumn("Units", format="%.2f"),
                "Result": st.column_config.MarkdownColumn("Result"),
            },
            hide_index=True,
            height=480,
        )
        st.caption(
            f"{len(picks_board)} pick(s) · "
            f"{board_metrics.pending} pending · "
            f"{board_metrics.units_risked:.2f}u risked"
        )

# ==================================================================
# Tab 2: Official Picks
# ==================================================================
with tabs[1]:
    st.subheader("Official Picks (Frozen Snapshots)")
    st.caption("Official picks meet all qualification thresholds and are frozen as immutable records. Variable Kelly staking.")

    try:
        import pandas as pd
        conn = _open_dashboard_connection(db_path)
        try:
            op_rows = conn.execute("""
                SELECT op.*, hr.player_name, hr.market_type, hr.market_form,
                       hr.side, hr.line, hr.sportsbook, hr.offered_american_odds,
                       hr.ev_pct, hr.yn_implied_prob_adv, hr.n_consensus_books,
                       hr.matchup, hr.event_status, hr.event_start_time,
                       hr.model_score, hr.score_explanation
                FROM official_picks op
                JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
                WHERE op.tier = 'OFFICIAL_TRACKED'
                ORDER BY op.official_rank
            """).fetchall()
            official_picks = [dict(r) for r in op_rows]
        finally:
            conn.close()

        if official_picks:
            op_table = []
            for op in official_picks:
                mform = op.get("market_form", "")
                is_yn = mform == "yn"
                ev_d = round(op["ev_pct"], 2) if not is_yn and op.get("ev_pct") is not None else ""
                pa_d = round(op["yn_implied_prob_adv"], 2) if is_yn and op.get("yn_implied_prob_adv") is not None else ""
                outcome = op.get("outcome", "pending")
                profit = op.get("profit_units")
                op_table.append({
                    "Rank": op.get("official_rank", ""),
                    "Player": op.get("player_name", ""),
                    "Market": _format_market_type(op.get("market_type", "")),
                    "Side": op.get("side", ""),
                    "Line": op.get("line", ""),
                    "Sportsbook": op.get("sportsbook", ""),
                    "Odds": op.get("offered_american_odds", ""),
                    "EV %": ev_d,
                    "Price Adv (pp)": pa_d,
                    "Model Score": round(op["model_score"], 1) if op.get("model_score") is not None else "N/A",
                    "Outcome": outcome,
                    "Profit (u)": profit if profit is not None else "",
                    "Frozen At": (op.get("selected_at") or "")[:16],
                })

            metrics = st.columns(5, border=True)
            pending = sum(1 for o in official_picks if o.get("outcome") == "pending")
            wins = sum(1 for o in official_picks if o.get("outcome") == "win")
            losses = sum(1 for o in official_picks if o.get("outcome") == "loss")
            pushes = sum(1 for o in official_picks if o.get("outcome") == "push")
            total_profit = sum(o.get("profit_units") or 0 for o in official_picks if o.get("outcome") != "pending")
            metrics[0].metric("Total", len(official_picks))
            metrics[1].metric("Pending", pending)
            metrics[2].metric("Wins", wins)
            metrics[3].metric("Losses", losses)
            metrics[4].metric("Profit (u)", round(total_profit, 2))

            st.dataframe(pd.DataFrame(op_table), use_container_width=True, hide_index=True)
        else:
            st.info("No official picks yet. Run the pipeline to generate them.")
    except Exception as e:
        st.error(f"Error loading official picks: {e}")

    # Why No Official Picks Today
    st.divider()
    st.subheader(":material/help: Why No Official Picks Today")
    try:
        import pandas as pd
        conn_why = _open_dashboard_connection(db_path)
        try:
            today_recs = conn_why.execute(
                "SELECT * FROM historical_recommendations "
                "WHERE date(scan_timestamp) = date('now') "
                "AND event_status NOT IN ('live','inprogress','in_progress','started','in-progress',"
                "'final','finished','completed','closed','ended') "
                "ORDER BY model_score DESC LIMIT 20"
            ).fetchall()

            official_today = [r for r in today_recs if dict(r).get("recommendation_tier") == "OFFICIAL_TRACKED"]

            if official_today:
                st.success(f"{len(official_today)} official pick(s) qualified today.")
            elif today_recs:
                why_data = []
                for row in today_recs:
                    r = dict(row)
                    score = r.get("model_score")
                    disq = r.get("disqualification_reasons", "")
                    pts_to_7 = r.get("points_to_7", 0.0)
                    price_cap = r.get("price_outlier_capped", 0)
                    true_ev = r.get("true_ev_unavailable", 0)
                    one_sided = r.get("one_sided_market", 0)
                    insuff = r.get("insufficient_books_failure", 0)

                    # Parse score components
                    comps = {}
                    try:
                        comps = json.loads(r.get("score_components") or "{}")
                    except Exception:
                        pass

                    why_data.append({
                        "Player": r.get("player_name", ""),
                        "Market": _format_market_type(r.get("market_type", "")),
                        "Score": round(score, 1) if score else 0,
                        "Pts to 7.0": round(pts_to_7, 2) if pts_to_7 else 0,
                        "Value": round(comps.get("value", 0) * 8.8, 1),
                        "Market Q": round(comps.get("market_quality", 0) * 8.8, 1),
                        "Reliability": round(comps.get("reliability", 0) * 8.8, 1),
                        "Freshness": round(comps.get("freshness", 0) * 8.8, 1),
                        "Confidence": round(comps.get("confidence", 0) * 8.8, 1),
                        "Risk": round(comps.get("risk", 0) * 8.8, 1),
                        "Books": r.get("n_consensus_books", 0),
                        "Price Cap": "Yes" if price_cap else "",
                        "No True EV": "Yes" if true_ev else "",
                        "One-Sided": "Yes" if one_sided else "",
                        "Insuff. Books": "Yes" if insuff else "",
                        "Failed Gates": disq[:80] if disq else "",
                    })

                st.dataframe(pd.DataFrame(why_data), use_container_width=True, hide_index=True)
                st.caption("Showing top 20 research picks by Model Score. Failed gates explain why they did not reach 7.0.")
            else:
                st.info("No recommendations generated today yet.")
        finally:
            conn_why.close()
    except Exception as e:
        st.error(f"Error loading diagnostics: {e}")

    # Manual grading
    st.divider()
    st.subheader(":material/fact_check: Manual Grading")
    if st.button("Grade Pending Official Picks", use_container_width=False):
        try:
            from src.tracker import grade_pending_picks
            conn = _open_dashboard_connection(db_path)
            try:
                graded = grade_pending_picks(conn)
                st.success(f"Graded {graded} official pick(s)")
            finally:
                conn.close()
        except Exception as e:
            st.error(f"Grading failed: {e}")

# ==================================================================
# Tab 3: Research
# ==================================================================
with tabs[2]:
    st.subheader(":material/science: Research Recommendations")
    st.caption("Discovery picks (score >= 6.0, private research) and Research-only picks are for threshold calibration.")
    st.caption("Model Score is a quality metric — not a guaranteed win probability. It does not predict game outcomes.")

    recs_all = _load_recs(db_path, "today")
    discovery_recs = [r for r in recs_all if r.get("recommendation_tier") == "DISCOVERY_TRACKED"]
    research_only = [r for r in recs_all if r.get("recommendation_tier") == "RESEARCH_ONLY"]

    # Discovery tier summary
    if discovery_recs:
        st.subheader("Discovery Picks (Private Research)")
        st.caption(f"{len(discovery_recs)} pick(s) scored >= 6.0 — does not count toward official record.")
        disc_cols = st.columns(3, border=True)
        disc_cols[0].metric("Discovery", len(discovery_recs))
        disc_cols[1].metric("Research Only", len(research_only))
        disc_cols[2].metric("Total Non-Official", len(discovery_recs) + len(research_only))

    all_non_official = discovery_recs + research_only

    if not all_non_official:
        st.info("No research-only recommendations today.")
    else:
        try:
            import pandas as pd
            filter_cols = st.columns(4)
            with filter_cols[0]:
                from src.prop_config import MARKET_REGISTRY
                market_options = {"All": None}
                for market_config in MARKET_REGISTRY:
                    types = tuple(
                        value for value in (market_config.market_type_ou, market_config.market_type_yn)
                        if value
                    )
                    label = f"{market_config.cli_name} ({', '.join(types)})"
                    market_options[label] = set(types)
                sel_market = st.selectbox(
                    "Market", list(market_options), key="research_filter_market",
                )
                st.caption("Registry markets remain selectable even when no rows survived the current scan.")
            with filter_cols[1]:
                books = sorted(set(r.get("sportsbook", "") for r in all_non_official if r.get("sportsbook")))
                sel_book = st.selectbox("Sportsbook", ["All"] + books, key="research_filter_book")
            with filter_cols[2]:
                score_filter = st.selectbox("Model Score", ["All", "6.0+", "5.5+", "Below 6.0"], key="research_filter_score")
            with filter_cols[3]:
                tier_filter = st.selectbox("Tier", ["All", "DISCOVERY_TRACKED", "RESEARCH_ONLY"], key="research_filter_tier")

            filtered = all_non_official
            selected_types = market_options[sel_market]
            if selected_types is not None:
                filtered = [r for r in filtered if r.get("market_type") in selected_types]
            if sel_book != "All":
                filtered = [r for r in filtered if r.get("sportsbook") == sel_book]
            if score_filter == "6.0+":
                filtered = [r for r in filtered if (r.get("model_score") or 0) >= 6.0]
            elif score_filter == "5.5+":
                filtered = [r for r in filtered if (r.get("model_score") or 0) >= 5.5]
            elif score_filter == "Below 6.0":
                filtered = [r for r in filtered if (r.get("model_score") or 0) < 6.0]
            if tier_filter != "All":
                filtered = [r for r in filtered if r.get("recommendation_tier") == tier_filter]

            filtered.sort(key=lambda r: -(r.get("model_score") or 0))

            table_data = []
            for r in filtered:
                mform = r.get("market_form", "")
                is_yn = mform == "yn"
                ev_d = round(r["ev_pct"], 2) if not is_yn and r.get("ev_pct") is not None else ""
                pa_d = round(r["yn_implied_prob_adv"], 2) if is_yn and r.get("yn_implied_prob_adv") is not None else ""
                table_data.append({
                    "Player": r.get("player_name", ""),
                    "Market": _format_market_type(r.get("market_type", "")),
                    "Side": r.get("side", ""),
                    "Line": r.get("line", ""),
                    "Sportsbook": r.get("sportsbook", ""),
                    "Odds": r.get("offered_american_odds", ""),
                    "EV %": ev_d,
                    "Price Adv (pp)": pa_d,
                    "Model Score": round(r["model_score"], 1) if r.get("model_score") is not None else "N/A",
                    "Status": r.get("rec_status", ""),
                    "Matchup": r.get("matchup", ""),
                    "Reason": (r.get("disqualification_reasons") or "")[:80],
                })

            if table_data:
                st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)
                st.caption(f"Showing {len(filtered)} of {len(research_only)} research picks")
            else:
                if selected_types is not None:
                    try:
                        coverage_conn = _open_dashboard_connection(db_path)
                        try:
                            placeholders = ",".join("?" * len(selected_types))
                            coverage_rows = coverage_conn.execute(
                                f"""SELECT market_type, market_group_key, side,
                                           player_id, sportsbook
                                    FROM player_prop_odds
                                    WHERE date(captured_at) = date('now')
                                      AND market_type IN ({placeholders})
                                      AND validation_status IN ('VALID','CONFIRMED','VERIFIED')""",
                                list(selected_types),
                            ).fetchall()
                        finally:
                            coverage_conn.close()
                        coverage_rows = [dict(row) for row in coverage_rows]
                        if coverage_rows:
                            groups = {}
                            for row in coverage_rows:
                                key = row.get("market_group_key")
                                groups.setdefault(key, set()).add((row.get("side") or "").upper())
                            ou_rows = [r for r in coverage_rows if str(r.get("market_type", "")).endswith("_ou")]
                            yn_rows = [r for r in coverage_rows if str(r.get("market_type", "")).endswith("_yn")]
                            paired = sum(1 for sides in groups.values() if {"OVER", "UNDER"}.issubset(sides))
                            st.info(
                                f"No saved research picks for this market. Raw approved coverage: "
                                f"{len(coverage_rows)} rows, {len({r.get('player_id') for r in coverage_rows})} players, "
                                f"{len({r.get('sportsbook') for r in coverage_rows})} books. "
                                f"O/U rows={len(ou_rows)}, Y/N rows={len(yn_rows)}, "
                                f"exact groups={len(groups)}, paired O/U groups={paired}. "
                                "The opportunities did not survive the recommendation filters."
                            )
                        else:
                            st.warning(
                                "No approved raw rows were recorded for this market today. "
                                "This indicates API/parser coverage, not simply no positive edge."
                            )
                    except Exception:
                        st.info("No research picks match filters.")
                else:
                    st.info("No research picks match filters.")
        except Exception as e:
            st.error(f"Error: {e}")

# ==================================================================
# Tab 4: Line Movement
# ==================================================================
with tabs[3]:
    st.subheader(":material/trending_up: Odds Observations & Line Movement")
    st.caption("Track odds changes from morning → pregame → closing for official picks.")

    try:
        import pandas as pd
        conn = _open_dashboard_connection(db_path)
        try:
            official_rows = conn.execute("""
                SELECT op.recommendation_id, hr.player_name, hr.market_type, hr.side,
                       hr.line, hr.sportsbook, hr.matchup, op.selected_at
                FROM official_picks op
                JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
                WHERE date(op.selected_at) = date('now')
                ORDER BY op.official_rank
            """).fetchall()
            official_picks_list = [dict(r) for r in official_rows]
        finally:
            conn.close()

        if not official_picks_list:
            st.info("No official picks today to track observations for.")
        else:
            for op in official_picks_list:
                rid = op["recommendation_id"]
                label = f"{op.get('player_name', '?')} — {op.get('market_type', '?')} ({op.get('side', '?')}) @ {op.get('sportsbook', '?')}"
                with st.expander(label, expanded=False):
                    try:
                        conn2 = _open_dashboard_connection(db_path)
                        try:
                            obs_rows = conn2.execute("""
                                SELECT * FROM pick_observations
                                WHERE official_pick_id = ?
                                ORDER BY observed_at
                            """, (rid,)).fetchall()
                            observations = [dict(r) for r in obs_rows]
                        finally:
                            conn2.close()

                        if not observations:
                            st.info("No observations recorded yet.")
                        else:
                            obs_table = []
                            for obs in observations:
                                obs_table.append({
                                    "Type": obs.get("observation_type", ""),
                                    "Sportsbook": obs.get("sportsbook", ""),
                                    "Odds": obs.get("american_odds", ""),
                                    "Line": obs.get("line", ""),
                                    "Implied Prob": round(obs.get("implied_prob", 0), 4) if obs.get("implied_prob") else "",
                                    "Unique Books": obs.get("unique_book_count", ""),
                                    "Freshness": obs.get("freshness_status", ""),
                                    "Observed At": (obs.get("observed_at") or "")[:16],
                                })

                            st.dataframe(pd.DataFrame(obs_table), use_container_width=True, hide_index=True)

                            if len(observations) >= 2:
                                conn_mv = _open_dashboard_connection(db_path)
                                try:
                                    from src.observations import compute_movement
                                    movement = compute_movement(conn_mv, rid)
                                    if movement.get("odds_movement_morning_to_pregame") is not None:
                                        st.info(f"Odds movement (morning→pregame): {movement['odds_movement_morning_to_pregame']:+d} cents")
                                    if movement.get("odds_movement_pregame_to_closing") is not None:
                                        st.info(f"Odds movement (pregame→closing): {movement['odds_movement_pregame_to_closing']:+d} cents")
                                finally:
                                    conn_mv.close()
                    except Exception as e:
                        st.error(f"Error: {e}")
    except Exception as e:
        st.error(f"Error loading observations: {e}")

# ==================================================================
# Tab 5: Performance
# ==================================================================
with tabs[4]:
    st.subheader(":material/analytics: Tracker & Performance")
    st.caption("Variable Kelly staking (25% fractional Kelly × score multiplier). P/L in units.")

    try:
        import pandas as pd
        from src.tracker import compute_performance, breakdown_by_field, get_official_picks

        conn_perf = _open_dashboard_connection(db_path)
        try:
            official_all = get_official_picks(conn_perf)
            metrics = compute_performance(conn_perf)
        finally:
            conn_perf.close()

        m_cols = st.columns(5, border=True)
        m_cols[0].metric("Total Picks", metrics.total)
        m_cols[1].metric("Wins", metrics.wins)
        m_cols[2].metric("Losses", metrics.losses)
        m_cols[3].metric("Pushes", metrics.pushes)
        m_cols[4].metric("Total Profit (u)", round(metrics.units_won, 2))

        b_cols = st.columns(3, border=True)
        b_cols[0].metric("Win Rate", f"{metrics.win_rate:.1%}")
        b_cols[1].metric("ROI", f"{metrics.roi:.1%}")
        b_cols[2].metric("Avg EV", f"{metrics.avg_ev:.2f}%")

        st.divider()

        # ── Cumulative PnL Chart ──
        try:
            conn_chart = _open_dashboard_connection(db_path)
            try:
                rows = conn_chart.execute("""
                    SELECT op.selected_at, op.profit_units
                    FROM official_picks op
                    WHERE op.outcome IN ('win', 'loss')
                    ORDER BY op.selected_at ASC
                """).fetchall()
            finally:
                conn_chart.close()
            if rows:
                import altair as alt

                df_pnl = pd.DataFrame({
                    "Date": [r["selected_at"][:10] for r in rows],
                    "Profit": [r["profit_units"] or 0 for r in rows],
                })
                df_pnl["Cumulative"] = df_pnl["Profit"].cumsum()
                df_pnl["Date"] = pd.to_datetime(df_pnl["Date"])
                st.subheader("Cumulative Profit / Loss")
                base = alt.Chart(df_pnl)
                area = base.mark_area(color="#B9FF45", opacity=0.14).encode(
                    x=alt.X("Date:T", title="", axis=alt.Axis(format="%b %d")),
                    y=alt.Y("Cumulative:Q", title="Cumulative PnL (u)"),
                )
                line = base.mark_line(stroke="#B9FF45", strokeWidth=2.5).encode(
                    x=alt.X("Date:T", title="", axis=alt.Axis(format="%b %d")),
                    y=alt.Y("Cumulative:Q", title="Cumulative PnL (u)"),
                )
                zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                    stroke="rgba(255,255,255,0.15)"
                ).encode(y="y:Q")
                st.altair_chart(
                    _theme_chart((area + line + zero), height=340),
                    use_container_width=True,
                )
        except Exception as e:
            st.caption(f"PnL chart unavailable: {e}")

        # ── EV vs Actual Profit ──
        try:
            conn_ev = _open_dashboard_connection(db_path)
            try:
                ev_rows = conn_ev.execute("""
                    SELECT op.profit_units, hr.ev_pct
                    FROM official_picks op
                    JOIN historical_recommendations hr ON op.recommendation_id = hr.recommendation_id
                    WHERE op.outcome IN ('win', 'loss')
                      AND hr.ev_pct IS NOT NULL
                """).fetchall()
            finally:
                conn_ev.close()
            if ev_rows and len(ev_rows) >= 3:
                import altair as alt

                df_ev = pd.DataFrame({
                    "EV%": [r["ev_pct"] for r in ev_rows],
                    "Profit (u)": [r["profit_units"] or 0 for r in ev_rows],
                })
                df_ev["Sign"] = df_ev["Profit (u)"].apply(
                    lambda p: "#B9FF45" if p >= 0 else "#FF3D58"
                )
                st.subheader("EV% vs Profit")
                scatter = alt.Chart(df_ev).mark_circle(size=70, opacity=0.9).encode(
                    x=alt.X("EV%:Q", title="Expected Value %"),
                    y=alt.Y("Profit (u):Q", title="Profit (units)"),
                    color=alt.Color("Sign:N", legend=None),
                    tooltip=["EV%", "Profit (u)"],
                )
                zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(
                    stroke="rgba(255,255,255,0.15)"
                ).encode(y="y:Q")
                st.altair_chart(
                    _theme_chart((zero + scatter), height=340),
                    use_container_width=True,
                )
        except Exception as e:
            st.caption(f"EV chart unavailable: {e}")

        st.divider()

        breakdown_fields = ["market_type", "sportsbook", "market_form"]
        for field in breakdown_fields:
            try:
                conn_bd = _open_dashboard_connection(db_path)
                try:
                    bd = breakdown_by_field(conn_bd, field)
                finally:
                    conn_bd.close()
                if bd:
                    st.subheader(f"Breakdown by {field.replace('_', ' ').title()}")
                    st.dataframe(pd.DataFrame(bd), use_container_width=True, hide_index=True)
            except Exception:
                pass

        if not official_all:
            st.info("No official picks with outcomes yet.")
    except Exception as e:
        st.error(f"Error loading performance data: {e}")

# ==================================================================
# Tab 6: Market Intelligence
# ==================================================================
with tabs[5]:
    st.subheader(":material/insights: Market Intelligence")

    _conn_mi = _open_dashboard_connection(db_path)
    try:
        _today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Load all odds rows from today
        _mi_rows = _conn_mi.execute(
            "SELECT market AS market_type, event_id, NULL AS player_id, NULL AS player_name, sportsbook, "
            "'VALID' AS validation_status, '' AS mapping_confidence, is_alt_line, available, "
            "pulled_at AS captured_at, points AS line, selection AS side "
            "FROM odds WHERE date(pulled_at) = ?",
            (_today_str,),
        ).fetchall()

        # Load today's recommendations
        _mi_recs = _conn_mi.execute(
            "SELECT market_type, recommendation_tier, model_score, event_id, "
            "player_id, sportsbook, n_consensus_books "
            "FROM historical_recommendations WHERE date(scan_timestamp) = ?",
            (_today_str,),
        ).fetchall()

        if not _mi_rows:
            st.info("No market data available for today.")
        else:
            # Aggregate by market_type
            from collections import defaultdict
            market_stats: dict[str, dict] = defaultdict(lambda: {
                "total_odds_rows": 0,
                "unique_events": set(),
                "unique_players": set(),
                "unique_sportsbooks": set(),
                "books_per_market": defaultdict(set),
                "stale_count": 0,
                "mapping_failures": 0,
                "official_count": 0,
                "discovery_count": 0,
                "research_count": 0,
            })

            for row in _mi_rows:
                mt = row["market_type"] or "unknown"
                s = market_stats[mt]
                s["total_odds_rows"] += 1
                s["unique_events"].add(row["event_id"])
                s["unique_players"].add(row["player_id"])
                s["unique_sportsbooks"].add(row["sportsbook"])
                # Track books per exact market (event+player+line+side)
                exact_key = f"{row['event_id']}|{row['player_id']}|{row['line']}|{row['side']}"
                s["books_per_market"][exact_key].add(row["sportsbook"])
                if row["validation_status"] == "STALE":
                    s["stale_count"] += 1
                if row["mapping_confidence"] in ("LOW", "NONE", "FAILED", "REJECTED"):
                    s["mapping_failures"] += 1

            for rec in _mi_recs:
                mt = rec["market_type"] or "unknown"
                tier = rec["recommendation_tier"] or "RESEARCH_ONLY"
                s = market_stats[mt]
                if tier == "OFFICIAL_TRACKED":
                    s["official_count"] += 1
                elif tier == "DISCOVERY_TRACKED":
                    s["discovery_count"] += 1
                else:
                    s["research_count"] += 1

            # Build display rows
            mi_display = []
            for mt, s in sorted(market_stats.items()):
                n_exact = len(s["books_per_market"])
                books_list = [len(v) for v in s["books_per_market"].values()]
                avg_books = round(sum(books_list) / max(1, len(books_list)), 1)
                median_books = sorted(books_list)[len(books_list) // 2] if books_list else 0
                pct_4plus = round(100.0 * sum(1 for b in books_list if b >= 4) / max(1, len(books_list)), 1)
                # Complete two-sided: markets with both over and under represented
                two_sided = sum(1 for v in s["books_per_market"].values() if len(v) >= 2)

                # Look up display name from registry
                from src.prop_config import get_market_by_ou_type, get_market_by_yn_type
                cfg = get_market_by_ou_type(mt) or get_market_by_yn_type(mt)
                display_name = cfg.display_name if cfg else mt

                mi_display.append({
                    "Market": display_name,
                    "Type": mt,
                    "Odds Rows": s["total_odds_rows"],
                    "Events": len(s["unique_events"]),
                    "Players": len(s["unique_players"]),
                    "Books": len(s["unique_sportsbooks"]),
                    "Avg Books/Market": avg_books,
                    "Median Books": median_books,
                    "4+ Books %": pct_4plus,
                    "Two-Sided": two_sided,
                    "Stale": s["stale_count"],
                    "Map Fail": s["mapping_failures"],
                    "Official": s["official_count"],
                    "Discovery": s["discovery_count"],
                    "Research": s["research_count"],
                })

            # Sort by average sportsbook coverage desc, then discovery count desc
            mi_display.sort(key=lambda x: (-x["Avg Books/Market"], -x["Discovery"], -x["Research"]))

            import pandas as pd
            st.dataframe(pd.DataFrame(mi_display), use_container_width=True, hide_index=True)

            # Summary
            total_official = sum(s["official_count"] for s in market_stats.values())
            total_discovery = sum(s["discovery_count"] for s in market_stats.values())
            total_research = sum(s["research_count"] for s in market_stats.values())
            st.caption(
                f"Total: {len(market_stats)} markets | "
                f"Official: {total_official} | Discovery: {total_discovery} | Research: {total_research}"
            )

            # Top markets by Market Quality Score
            st.subheader("Top Markets by Market Quality Score")
            _mqs_rows = _conn_mi.execute(
                "SELECT market_type, market_quality_score, model_score, "
                "n_consensus_books, recommendation_tier "
                "FROM historical_recommendations "
                "WHERE date(scan_timestamp) = ? AND market_quality_score > 0 "
                "ORDER BY market_quality_score DESC LIMIT 20",
                (_today_str,),
            ).fetchall()
            if _mqs_rows:
                mqs_display = []
                for row in _mqs_rows:
                    from src.prop_config import get_market_by_ou_type as _gou, get_market_by_yn_type as _gyn
                    cfg2 = _gou(row["market_type"]) or _gyn(row["market_type"])
                    mqs_display.append({
                        "Market": cfg2.display_name if cfg2 else row["market_type"],
                        "MQS": round(row["market_quality_score"], 2),
                        "Model Score": round(row["model_score"], 1) if row["model_score"] else 0,
                        "Books": row["n_consensus_books"],
                        "Tier": row["recommendation_tier"] or "RESEARCH_ONLY",
                    })
                st.dataframe(pd.DataFrame(mqs_display), use_container_width=True, hide_index=True)
            else:
                st.info("No Market Quality Score data available yet.")

    finally:
        _conn_mi.close()

# ==================================================================
# Tab 7: Run & Operations
# ==================================================================
with tabs[6]:
    st.subheader(":material/settings: Run & Operations")
    st.caption("Pipeline control, today's schedule, automation health, and system diagnostics.")

    # ── Deployment status ─────────────────────────────────────────
    deploy_cols = st.columns(4, border=True)
    with deploy_cols[0]:
        env_label = config.environment if config else "local"
        st.metric("Environment", env_label.upper())
    with deploy_cols[1]:
        sched_status = "ENABLED" if (config and config.scheduler_enabled) else "DISABLED"
        st.metric("Scheduler", sched_status)
    with deploy_cols[2]:
        shadow_label = "SHADOW" if (config and config.shadow_mode) else "LIVE"
        st.metric("Mode", shadow_label)
    with deploy_cols[3]:
        st.metric("Timezone", config.timezone if config else "UTC")

    # ── Worker heartbeat ──────────────────────────────────────────
    try:
        hb_conn = _open_dashboard_connection(db_path)
        try:
            hb_row = hb_conn.execute(
                "SELECT last_heartbeat, worker_pid FROM worker_heartbeat WHERE id = 1"
            ).fetchone()
        finally:
            hb_conn.close()

        hb_cols = st.columns(3, border=True)
        with hb_cols[0]:
            if hb_row:
                st.metric("Worker Heartbeat", hb_row["last_heartbeat"][:19] if hb_row["last_heartbeat"] else "Never")
            else:
                st.metric("Worker Heartbeat", "Not started")
        with hb_cols[1]:
            st.metric("Worker PID", hb_row["worker_pid"] if hb_row and hb_row["worker_pid"] else "—")
        with hb_cols[2]:
            # Check if heartbeat is stale
            if hb_row and hb_row["last_heartbeat"]:
                from datetime import datetime as _dt, timezone as _tz
                hb_time = _dt.fromisoformat(hb_row["last_heartbeat"])
                if hb_time.tzinfo is None:
                    hb_time = hb_time.replace(tzinfo=_tz.utc)
                age_s = (_dt.now(_tz.utc) - hb_time).total_seconds()
                if age_s > 300:
                    st.metric("Worker Status", "STALE")
                else:
                    st.metric("Worker Status", "ACTIVE")
            else:
                st.metric("Worker Status", "UNKNOWN")
    except Exception:
        st.info("Worker heartbeat not available")

    st.divider()

    # ── Pipeline ──────────────────────────────────────────────────
    st.subheader(":material/play_arrow: Pipeline")
    latest_rid = _get_latest_run_id(db_path)
    st.markdown(f":gray[**Latest Run ID**]  {latest_rid[:12] if latest_rid else 'None'}")
    can_run = True
    if st.session_state.last_run_time and not st.session_state.run_active:
        try:
            last = datetime.fromisoformat(st.session_state.last_run_time.replace(" UTC", " +00:00"))
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < 900:
                remaining = int((900 - elapsed) / 60) + 1
                st.warning(f"Last run was {int(elapsed / 60)} min ago. Wait {remaining} min before rerunning.")
                can_run = False
        except Exception:
            pass
    if st.session_state.run_active:
        st.info("⏳ Pipeline is running... please wait.")
        can_run = False

    btn_col1, btn_col2, btn_col3 = st.columns([3, 1, 1])
    with btn_col1:
        run_clicked = st.button("▶️  RUN TODAY'S MLB MODEL", type="primary", disabled=not can_run, use_container_width=True)
    with btn_col2:
        if st.session_state.run_active:
            st.button("⏹ Stop", disabled=True, use_container_width=True)
    with btn_col3:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    if run_clicked and can_run:
        st.session_state.run_active = True
        st.session_state.run_log = []
        st.session_state.run_result = None
        result: dict[str, Any] = {"status": "running", "steps": [], "exit_code": None, "output": "", "error": ""}
        st.session_state.run_result = result
        thread = threading.Thread(target=_run_pipeline_background, args=(output_dir_val, result), daemon=True)
        thread.start()
        thread.join(timeout=600)
        st.session_state.run_active = False
        st.session_state.last_run_time = _now_str()
        st.rerun()

    if st.session_state.run_result:
        r = st.session_state.run_result
        status = r.get("status", "unknown")
        exit_code = r.get("exit_code")
        output = r.get("output", "")
        error = r.get("error", "")
        if status == "success":
            st.success(f"Pipeline completed (exit code {exit_code})")
        elif status == "failed":
            st.error(f"Pipeline failed (exit code {exit_code})")
            if error:
                st.error(error)
        if output:
            with st.expander("📋 Pipeline Output", expanded=(status == "failed")):
                st.code(output, language=None)
        with st.expander("🔧 Technical Details", expanded=False):
            st.json({"exit_code": exit_code, "status": status, "timestamp": _now_str(), "database": "PostgreSQL" if _is_postgres() else db_path, "output_dir": output_dir_val, "error": error or None})

    # Pipeline completion indicator
    _pipeline_flag = Path(__file__).resolve().parent.parent / "database" / ".pipeline_completed"
    if _pipeline_flag.exists():
        try:
            _flag_data = json.loads(_pipeline_flag.read_text())
            _ts = _flag_data.get("timestamp", "unknown")[:19].replace("T", " ")
            _n = _flag_data.get("n_recommendations", 0)
            st.success(f"Pipeline completed at {_ts} — {_n} recommendation(s) saved")
        except Exception:
            pass

    st.divider()

    # ── Today's Schedule ──────────────────────────────────────────
    st.subheader(":material/calendar_month: Today's Schedule")
    run_summary_for_sched = _load_latest_run_summary(output_dir_val)
    schedule = _get_schedule_summary(db_path, run_summary_for_sched)

    sched_cols = st.columns(8, border=True)
    for i, (label, key) in enumerate([
        ("Total MLB Games", "total"), ("Upcoming", "upcoming"),
        ("Live", "live"), ("Completed", "completed"),
        ("Postponed", "postponed"), ("Cancelled", "cancelled"),
        ("Analyzed", "analyzed"), ("Skipped", "skipped"),
    ]):
        sched_cols[i].metric(label, schedule[key])

    parts_sum = (
        schedule["upcoming"] + schedule["live"] + schedule["completed"]
        + schedule["postponed"] + schedule["cancelled"]
    )
    if schedule["total"] > 0 and parts_sum != schedule["total"]:
        st.warning(
            f"Schedule count mismatch: Total ({schedule['total']}) ≠ "
            f"Upcoming + Live + Completed + Postponed + Cancelled ({parts_sum})"
        )
    if schedule["eligible"] > 0 and not schedule["valid"]:
        st.warning(
            f"Analysis count mismatch: Analyzed ({schedule['analyzed']}) + "
            f"Skipped ({schedule['skipped']}) ≠ Eligible ({schedule['eligible']})"
        )

    # Recommendations summary
    recs = _load_recs(db_path, "latest")
    if recs:
        official = [r for r in recs if r.get("recommendation_tier") == "OFFICIAL_TRACKED"]
        research_recs = [r for r in recs if r.get("recommendation_tier") != "OFFICIAL_TRACKED"]
        tier_cols = st.columns(3, border=True)
        tier_cols[0].metric("Official", len(official))
        tier_cols[1].metric("Research Only", len(research_recs))
        tier_cols[2].metric("Total", len(recs))

    # Skipped games
    st.subheader(":material/block: Skipped Games")
    run_summary_skipped = _load_latest_run_summary(output_dir_val)
    skipped_games = _get_deduplicated_skipped_games(run_summary_skipped)
    if skipped_games:
        import pandas as pd
        sg_data = [{"Matchup": sg.get("matchup", ""), "Start Time": sg.get("start_time", "")[:16], "Status": sg.get("status", ""), "Reason": sg.get("reason", "")} for sg in skipped_games]
        st.dataframe(pd.DataFrame(sg_data), use_container_width=True, hide_index=True)
        st.caption(f"{len(skipped_games)} game(s) skipped")
    else:
        st.info("No skipped games.")

    st.divider()

    # ── Automation & Scheduling ───────────────────────────────────
    st.subheader(":material/schedule: Automation & Scheduling")
    st.caption("Morning run at 9 AM ET, pregame at start-60min, postgame grading.")

    try:
        from src.automation import get_automation_status, schedule_pregame_checks, schedule_grading, trigger_morning_run, trigger_grading, get_pending_jobs, get_failed_jobs, retry_failed_jobs

        conn = _open_dashboard_connection(db_path)
        try:
            status = get_automation_status(conn)
        finally:
            conn.close()

        # ── Job metrics ───────────────────────────────────────────
        auto_cols = st.columns(4, border=True)
        auto_cols[0].metric("Next Morning Run", status.get("next_morning_run", "Not scheduled")[:16] if status.get("next_morning_run") else "Not scheduled")
        auto_cols[1].metric("Last Morning Run", status.get("last_morning_run", "Never")[:16] if status.get("last_morning_run") else "Never")
        auto_cols[2].metric("Pending Pregame", status.get("pending_pregame_checks", 0))
        auto_cols[3].metric("Failed Jobs", status.get("failed_jobs", 0))

        st.divider()

        # ── Database persistence status ───────────────────────────
        st.subheader("Database & Storage")
        db_cols = st.columns(3, border=True)
        with db_cols[0]:
            st.metric("Database", "PostgreSQL" if _is_postgres() else Path(db_path).name)
        with db_cols[1]:
            if _is_postgres():
                st.metric("DB Size", "Managed (PostgreSQL)")
            else:
                st.metric("DB Size", f"{Path(db_path).stat().st_size / 1024:.0f} KB" if Path(db_path).exists() else "N/A")
        with db_cols[2]:
            backup_dir = config.backup_dir if config else "backups"
            backup_count = len(list(Path(backup_dir).glob("mlb_backup_*"))) if Path(backup_dir).exists() else 0
            st.metric("Backups", backup_count)

        st.divider()

        # ── Manual triggers ───────────────────────────────────────
        st.subheader("Manual Triggers")
        st.caption("Manual actions require confirmation.")
        trig_cols = st.columns(4)
        with trig_cols[0]:
            if st.button("🌅 Run Full Slate Now", use_container_width=True):
                if st.session_state.get("_confirm_morning"):
                    conn3 = _open_dashboard_connection(db_path)
                    try:
                        jid = trigger_morning_run(conn3)
                        st.success(f"Morning run job created: {jid[:8]}")
                    finally:
                        conn3.close()
                    st.session_state["_confirm_morning"] = False
                else:
                    st.session_state["_confirm_morning"] = True
                    st.warning("Click again to confirm full-slate run")
        with trig_cols[1]:
            if st.button("📈 Schedule Pregame Checks", use_container_width=True):
                conn3 = _open_dashboard_connection(db_path)
                try:
                    count = schedule_pregame_checks(conn3)
                    st.success(f"Scheduled {count} pregame check(s)")
                finally:
                    conn3.close()
        with trig_cols[2]:
            if st.button("✅ Run Grading Now", use_container_width=True):
                if st.session_state.get("_confirm_grading"):
                    conn3 = _open_dashboard_connection(db_path)
                    try:
                        count = schedule_grading(conn3)
                        st.success(f"Grading jobs created: {count}")
                    finally:
                        conn3.close()
                    st.session_state["_confirm_grading"] = False
                else:
                    st.session_state["_confirm_grading"] = True
                    st.warning("Click again to confirm grading")
        with trig_cols[3]:
            if st.button("🔄 Retry Failed Jobs", use_container_width=True):
                conn3 = _open_dashboard_connection(db_path)
                try:
                    count = retry_failed_jobs(conn3)
                    st.success(f"Reset {count} failed job(s) to pending")
                finally:
                    conn3.close()

        st.divider()

        # ── Production schedule ───────────────────────────────────
        st.subheader("Production Schedule (America/New_York)")
        sched_data = [
            {"Time": "8:30 AM", "Job": "Schedule Refresh", "Type": "pregame-check"},
            {"Time": "9:00 AM", "Job": "Full-Slate Model Run", "Type": "morning-run"},
            {"Time": "Game-60min", "Job": "Pre-Game Check", "Type": "pregame-check"},
            {"Time": "Game-15min", "Job": "Final Odds Snapshot", "Type": "pregame-check"},
            {"Time": "Post-Game", "Job": "Grading Checks", "Type": "grading"},
            {"Time": "3:30 AM", "Job": "Backup & Maintenance", "Type": "backup"},
        ]
        st.dataframe(pd.DataFrame(sched_data), use_container_width=True, hide_index=True)

        st.divider()

        # ── Pending jobs ──────────────────────────────────────────
        st.subheader("Pending Jobs")
        conn4 = _open_dashboard_connection(db_path)
        try:
            pending = get_pending_jobs(conn4)
            failed = get_failed_jobs(conn4)
        finally:
            conn4.close()

        if pending:
            pj_table = [{"Job ID": p["job_id"][:8], "Type": p["job_type"], "Scheduled": (p.get("scheduled_at") or "")[:16], "Event": p.get("event_id", "")[:12] if p.get("event_id") else "—"} for p in pending[:20]]
            st.dataframe(pd.DataFrame(pj_table), use_container_width=True, hide_index=True)
        else:
            st.info("No pending jobs.")

        if failed:
            st.subheader("Failed Jobs")
            fj_table = [{"Job ID": f["job_id"][:8], "Type": f["job_type"], "Error": (f.get("error_message") or "")[:60], "Scheduled": (f.get("scheduled_at") or "")[:16]} for f in failed[:10]]
            st.dataframe(pd.DataFrame(fj_table), use_container_width=True, hide_index=True)
        else:
            st.info("No failed jobs.")

    except Exception as e:
        st.error(f"Error loading automation data: {e}")

    st.divider()

    # ── Safety warnings ───────────────────────────────────────────
    live_warnings = _get_live_game_warnings(db_path, latest_rid)
    if live_warnings:
        st.error(f"⚠️ VALIDATION WARNING: {len(live_warnings)} recommendation(s) from live or completed games.")
        with st.expander("Live-game recommendations", expanded=True):
            import pandas as pd
            warn_data = [{"Player": w.get("player_name", ""), "Market": w.get("market_type", ""), "Sportsbook": w.get("sportsbook", ""), "Matchup": w.get("matchup", ""), "Status": w.get("event_status", "")} for w in live_warnings]
            st.dataframe(pd.DataFrame(warn_data), use_container_width=True, hide_index=True)

    st.divider()

    # ── System Health ─────────────────────────────────────────────
    st.subheader(":material/monitor_heart: System Health")

    if config and config.api_key:
        st.markdown(f":gray[**API Key**]  Set ({_redact(config.api_key)})")
    else:
        st.markdown(":gray[**API Key**]  Not set")

    if "health_report" not in st.session_state:
        st.session_state.health_report = None
    if st.session_state.health_report is None:
        try:
            health_fn = _import_health()
            report = health_fn(
                db_path=db_path, api_key=config.api_key if config else "",
                output_dir=output_dir_val,
                freshness_threshold=config.freshness_threshold_seconds if config else 3600,
                environment=config.environment if config else "",
                timezone_name=config.timezone if config else "",
                backup_dir=config.backup_dir if config else "backups",
                scheduler_enabled=config.scheduler_enabled if config else True,
                scheduling_pregame_interval_minutes=(
                    config.scheduling_pregame_interval_minutes if config else 30
                ),
            )
            st.session_state.health_report = report
        except Exception:
            st.session_state.health_report = "UNKNOWN"

    health_report = st.session_state.health_report
    if health_report == "UNKNOWN":
        st.markdown(":gray[**UNKNOWN** — Health check has not been run yet.]")
    else:
        color = _status_color(health_report.overall_status)
        st.markdown(f":{color}[**{health_report.overall_status.upper()}**]")
        for chk in health_report.checks:
            chk_color = "green" if chk.status == "ok" else ("orange" if chk.status == "warning" else "red")
            st.markdown(f":{chk_color}[{chk.name}: {chk.message}]")

    if st.button("🔍 Run Health Check", use_container_width=False):
        with st.spinner("Running health check..."):
            try:
                health_fn = _import_health()
                report = health_fn(
                    db_path=db_path, api_key=config.api_key if config else "",
                    output_dir=output_dir_val,
                    freshness_threshold=config.freshness_threshold_seconds if config else 3600,
                    environment=config.environment if config else "",
                    timezone_name=config.timezone if config else "",
                    backup_dir=config.backup_dir if config else "backups",
                    scheduler_enabled=config.scheduler_enabled if config else True,
                    scheduling_pregame_interval_minutes=(
                        config.scheduling_pregame_interval_minutes if config else 30
                    ),
                )
                st.session_state.health_report = report
                st.json(report.to_dict())
            except Exception as exc:
                st.error(f"Health check failed: {exc}")

    st.divider()

    # Data quality
    st.subheader(":material/rule: Data Quality")
    if st.button("📊 View Data Quality Findings", use_container_width=False):
        try:
            from src.data_quality import get_critical_findings, init_findings_table
            conn5 = _open_dashboard_connection(db_path)
            try:
                init_findings_table(conn5)
                findings = get_critical_findings(conn5)
                if findings:
                    import pandas as pd
                    st.dataframe(pd.DataFrame(findings), use_container_width=True, hide_index=True)
                else:
                    st.success("No critical data quality findings in the last 24 hours.")
            finally:
                conn5.close()
        except Exception as exc:
            st.error(f"Data quality check failed: {exc}")

    st.divider()

    # Backup
    st.subheader(":material/save: Backup")
    bk_cols = st.columns(2)
    with bk_cols[0]:
        if st.button("💾 Create Backup", use_container_width=True):
            try:
                backup_fn, _ = _import_backup()
                backup_dir = _get_backup_dir(config) if config else Path("output/backups")
                backup_dir.mkdir(parents=True, exist_ok=True)
                bp = backup_fn(db_path=db_path, backup_dir=backup_dir, retention_count=config.backup_retention_count if config else 7, compress=config.backup_compression if config else False)
                st.success(f"Backup created: {bp.name}")
            except Exception as exc:
                st.error(f"Backup failed: {exc}")
    with bk_cols[1]:
        if st.button("📂 Open Output", use_container_width=True):
            output_path = Path(output_dir_val)
            st.write(f"Output directory: `{output_path.resolve()}`" if output_path.exists() else "Output directory does not exist")

    st.divider()

    # Advanced controls
    with st.expander(":material/tune: Advanced Controls", expanded=False):
        adv_cols = st.columns(3)

        with adv_cols[0]:
            st.markdown("**Canary Tests**")
            if st.button("🐤 No-Write Canary", use_container_width=True, key="canary_nw"):
                result_box: dict[str, Any] = {"status": "running", "exit_code": None, "output": "", "error": ""}
                _run_subprocess_command("Canary (no-write)", [sys.executable, "-m", "src.production_canary", "--no-write"], result_box)
                if result_box["status"] == "success":
                    st.success("Canary passed (no-write)")
                else:
                    st.error(f"Canary failed: {result_box.get('error', '')}")
            if st.button("🐤 Full Canary", use_container_width=True, key="canary_full"):
                result_box_f: dict[str, Any] = {"status": "running", "exit_code": None, "output": "", "error": ""}
                _run_subprocess_command("Canary (full)", [sys.executable, "-m", "src.production_canary"], result_box_f)
                if result_box_f["status"] == "success":
                    st.success("Canary passed")
                else:
                    st.error(f"Canary failed: {result_box_f.get('error', '')}")

        with adv_cols[1]:
            st.markdown("**Operations**")
            if st.button("🔄 Pregame Run", use_container_width=True, key="pregame_run_btn"):
                result_box_p: dict[str, Any] = {"status": "running", "exit_code": None, "output": "", "error": ""}
                _run_subprocess_command("Pregame Run", [sys.executable, "-m", "src.production_jobs", "pregame-run"], result_box_p)
                if result_box_p["status"] == "success":
                    st.success("Pregame run completed")
                else:
                    st.error(f"Pregame failed: {result_box_p.get('error', '')}")
            if st.button("💰 Capture Closing Prices", use_container_width=True, key="closing_prices_btn"):
                result_box4: dict[str, Any] = {"status": "running", "exit_code": None, "output": "", "error": ""}
                _run_subprocess_command(
                    "Closing Prices",
                    [sys.executable, "-c",
                     "from database.db_manager import capture_closing_prices; "
                     "from database.db_manager import get_connection; "
                     "conn = get_connection(); "
                     "recs = [dict(row) for row in conn.execute(\"SELECT * FROM historical_recommendations WHERE date(scan_timestamp) = date('now')\").fetchall()]; "
                     "print(f'Closing prices captured: {capture_closing_prices(conn, recs)}'); "
                     "conn.close()"],
                    result_box4,
                )
                if result_box4["status"] == "success":
                    st.success("Closing prices captured")
                else:
                    st.error(f"Failed: {result_box4.get('error', '')}")

        with adv_cols[2]:
            st.markdown("**Grading**")
            if st.button("✅ Grade All Recommendations", use_container_width=True, key="grade_all_btn"):
                result_box5: dict[str, Any] = {"status": "running", "exit_code": None, "output": "", "error": ""}
                _run_subprocess_command("Grade all", [sys.executable, "-m", "src.grade_recommendations", "--grade-all"], result_box5)
                if result_box5["status"] == "success":
                    st.success("Grading completed")
                else:
                    st.error(f"Grading failed: {result_box5.get('error', '')}")
                if result_box5.get("output"):
                    with st.expander("Output"):
                        st.code(result_box5["output"], language=None)
            if st.button("📋 View Traces", use_container_width=True, key="view_traces_btn"):
                try:
                    conn6 = _open_dashboard_connection(db_path)
                    try:
                        traces = conn6.execute(
                            "SELECT recommendation_id, step, message, timestamp FROM recommendation_traces ORDER BY timestamp DESC LIMIT 20"
                        ).fetchall()
                        if traces:
                            trace_data = [{"ID": t["recommendation_id"][:12], "Step": t["step"], "Message": t["message"], "Time": t["timestamp"]} for t in traces]
                            import pandas as pd
                            st.dataframe(pd.DataFrame(trace_data), use_container_width=True, hide_index=True)
                        else:
                            st.info("No traces found")
                    finally:
                        conn6.close()
                except Exception as exc:
                    st.info(f"Traces unavailable: {exc}")

    st.divider()

    # Delivery status
    st.subheader(":material/forward_to_inbox: Delivery Status")
    del_cols = st.columns(4, border=True)
    del_cols[0].metric("Shadow Delivery", "Blocked")
    del_cols[1].metric("VIP Delivery", "Blocked")
    del_cols[2].metric("Shadow Mode", "Enabled" if shadow and shadow.shadow_mode else "Disabled")
    del_cols[3].metric("Recommendations", schedule.get("recommendations", 0))

# ==================================================================
# Tab 8: Adaptive Learning
# ==================================================================
with tabs[7]:
    st.subheader(":material/psychology: Adaptive Learning & Model Calibration")

    _conn_al = _open_dashboard_connection(db_path)

    try:
        from src.adaptive_learning import (
            compute_grade_summary,
            compute_score_calibration,
            compute_performance_segments,
            generate_learning_recommendations,
            can_auto_change,
            run_holdout_validation,
            ADAPTIVE_LEARNING_VERSION,
        )

        _allowed, _reason = can_auto_change(_conn_al)

        # ── Safety Gate ──
        st.markdown("**System Status**")
        _gate_cols = st.columns(3, border=True)
        _gate_cols[0].metric(
            "Auto-Change Gate",
            "✅ Enabled" if _allowed else "🚫 Blocked",
            help=_reason,
        )
        _gate_cols[1].metric("Learning Version", ADAPTIVE_LEARNING_VERSION)
        _graded_row = _conn_al.execute(
            "SELECT COUNT(*) AS graded_count FROM historical_recommendations hr "
            "JOIN market_settlements ms ON hr.recommendation_id = ms.recommendation_id "
            "WHERE ms.settlement_status IS NOT NULL AND ms.settlement_status != 'UNRESOLVED'"
        ).fetchone()

        _graded_count = _graded_row["graded_count"] if _graded_row else 0
        _gate_cols[2].metric("Graded Recs", _graded_count)

        if not _allowed:
            st.warning(f"Auto-change blocked: {_reason}")

        st.divider()

        # ── Section 1: Data Readiness ──
        st.subheader("1. Data Readiness")
        _recs = _conn_al.execute(
            "SELECT recommendation_tier, COUNT(*) as cnt "
            "FROM historical_recommendations GROUP BY recommendation_tier"
        ).fetchall()
        if _recs:
            import pandas as pd
            _tier_df = pd.DataFrame([{"Tier": r["recommendation_tier"], "Count": r["cnt"]} for r in _recs])
            st.dataframe(_tier_df, use_container_width=True, hide_index=True)

            _days = _conn_al.execute(
                "SELECT DISTINCT substr(scan_timestamp, 1, 10) as day "
                "FROM historical_recommendations ORDER BY day"
            ).fetchall()
            st.caption(f"Total: {_graded_count} graded | {len(_days)} unique days")
        else:
            st.info("No historical recommendation data available.")

        st.divider()

        # ── Independent Challenger ──
        st.subheader("Independent Strikeout Challenger (Shadow Only)")
        from src.challenger_evaluation import evaluate_shadow_from_connection
        _challenger = evaluate_shadow_from_connection(_conn_al)
        _ch_cols = st.columns(4, border=True)
        _ch_cols[0].metric("Shadow Sample", _challenger.get("sample_size", 0))
        _ch_cols[1].metric("Brier", _challenger.get("brier_score", "-"))
        _ch_cols[2].metric("Realized ROI", _challenger.get("realized_roi", "-"))
        _ch_cols[3].metric("CLV", _challenger.get("average_clv_probability", "-"))
        st.caption("The challenger never changes production picks. It needs a sufficient settled sample before comparison is meaningful.")

        st.divider()

        # ── Section 2: Score Calibration ──
        st.subheader("2. Score Calibration")
        try:
            _cal = compute_score_calibration(_conn_al)
            if _cal and _cal.get("buckets"):
                import pandas as pd
                _cal_rows = []
                for b in _cal["buckets"]:
                    _cal_rows.append({
                        "Bucket": b.get("bucket_label", ""),
                        "Total": b.get("total", 0),
                        "Wins": b.get("wins", 0),
                        "Losses": b.get("losses", 0),
                        "Win Rate": f"{b.get('actual_win_rate', 0):.1%}" if b.get("actual_win_rate") is not None else "N/A",
                        "ROI": f"{b.get('roi', 0):.1%}" if b.get("roi") is not None else "N/A",
                        "Avg CLV": f"{b.get('avg_clv', 0):.4f}" if b.get("avg_clv") is not None else "N/A",
                        "Sufficient": "✅" if b.get("sample_sufficient") else "⚠️",
                    })
                st.dataframe(pd.DataFrame(_cal_rows), use_container_width=True, hide_index=True)

                if _cal.get("score_distribution"):
                    _dist = _cal["score_distribution"]
                    _mean = _dist.get("mean")
                    _median = _dist.get("median")
                    _stdev = _dist.get("stdev")
                    _dist_cols = st.columns(3, border=True)
                    _dist_cols[0].metric("Mean Score", f"{_mean:.2f}" if _mean is not None else "N/A")
                    _dist_cols[1].metric("Median Score", f"{_median:.2f}" if _median is not None else "N/A")
                    _dist_cols[2].metric("Std Dev", f"{_stdev:.2f}" if _stdev is not None else "N/A")
            else:
                st.info("Score calibration requires more graded data.")
        except Exception as e:
            st.info(f"Score calibration unavailable: {e}")

        st.divider()

        # ── Section 3: Grade Summary by Tier ──
        st.subheader("3. Performance by Tier")
        try:
            _grade_summary = compute_grade_summary(_conn_al)
            if _grade_summary:
                import pandas as pd
                _tier_perf_rows = []
                for tier_name, perf_dict in _grade_summary.items():
                    _tier_perf_rows.append({
                        "Tier": tier_name,
                        "Total": perf_dict.get("total", 0),
                        "Wins": perf_dict.get("wins", 0),
                        "Losses": perf_dict.get("losses", 0),
                        "Win Rate": f"{perf_dict.get('win_rate', 0):.1%}",
                        "ROI": f"{perf_dict.get('roi', 0):.1%}",
                        "Units Won": f"{perf_dict.get('units_won', 0):.2f}",
                        "Max Drawdown": f"{perf_dict.get('max_drawdown', 0):.2f}",
                    })
                st.dataframe(pd.DataFrame(_tier_perf_rows), use_container_width=True, hide_index=True)
            else:
                st.info("No tier performance data available.")
        except Exception as e:
            st.info(f"Grade summary unavailable: {e}")

        st.divider()

        # ── Section 4: Learning Recommendations ──
        st.subheader("4. Learning Recommendations")
        try:
            _learn_recs = generate_learning_recommendations(_conn_al)
            if _learn_recs:
                import pandas as pd
                _lr_rows = []
                for lr in _learn_recs:
                    _lr_rows.append({
                        "Category": lr.get("category", ""),
                        "Proposed Change": lr.get("proposed_change", ""),
                        "Current": lr.get("current_value", ""),
                        "Proposed": lr.get("proposed_value", ""),
                        "Sample Size": lr.get("sample_size", 0),
                        "Status": lr.get("status", ""),
                        "Overfit Risk": lr.get("overfitting_risk", ""),
                    })
                st.dataframe(pd.DataFrame(_lr_rows), use_container_width=True, hide_index=True)
                st.caption(f"Total: {len(_learn_recs)} recommendations")
            else:
                st.info("No learning recommendations available (insufficient data or all thresholds met).")
        except Exception as e:
            st.info(f"Learning recommendations unavailable: {e}")

        st.divider()

        # ── Section 5: Holdout Validation ──
        st.subheader("5. Champion vs Challenger")
        try:
            _holdout = run_holdout_validation(_conn_al)
            if _holdout.get("champion_holdout"):
                _hv_cols = st.columns(3, border=True)
                _ch_train = _holdout.get("champion_train", {})
                _ch_holdout = _holdout.get("champion_holdout", {})
                _hv_cols[0].metric("Train ROI", f"{_ch_train.get('roi', 0):.1%}")
                _hv_cols[1].metric("Holdout ROI", f"{_ch_holdout.get('roi', 0):.1%}")
                _hv_cols[2].metric(
                    "Validated",
                    "✅ Yes" if _holdout.get("validated") else "⏳ Pending",
                )
                st.caption(
                    f"Train: {_holdout.get('train_size', 0)} recs | "
                    f"Val: {_holdout.get('val_size', 0)} recs | "
                    f"Holdout: {_holdout.get('holdout_size', 0)} recs"
                )
            else:
                st.info("Holdout validation requires more graded data for chronological split.")
        except Exception as e:
            st.info(f"Holdout validation unavailable: {e}")

        st.divider()

        # ── Section 6: Experiments ──
        st.subheader("6. Experiments")
        try:
            _experiments = _conn_al.execute(
                "SELECT experiment_id, challenger_id, conclusion, approved, created_at "
                "FROM experiments ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
            if _experiments:
                import pandas as pd
                _exp_rows = []
                for exp in _experiments:
                    _exp_rows.append({
                        "Experiment ID": exp["experiment_id"][:12] + "...",
                        "Challenger": exp["challenger_id"],
                        "Conclusion": exp["conclusion"],
                        "Approved": "✅" if exp["approved"] else "⏳",
                        "Created": exp["created_at"][:16] if exp["created_at"] else "",
                    })
                st.dataframe(pd.DataFrame(_exp_rows), use_container_width=True, hide_index=True)
            else:
                st.info("No experiments have been created yet.")
        except Exception as e:
            st.info(f"Experiments unavailable: {e}")

    finally:
        _conn_al.close()

# ── Footer ─────────────────────────────────────────────────────────
st.divider()
st.markdown("")
with st.container(horizontal_alignment="center"):
    st.markdown(":gray[**MLB VIP Model** · Shadow Mode — no wagers are placed · No deliveries are sent]")
    st.caption(f"Database: {'PostgreSQL' if _is_postgres() else db_path} · Updated: {_now_str()}")
