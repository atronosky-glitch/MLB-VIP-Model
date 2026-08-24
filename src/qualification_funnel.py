"""Qualification-funnel visibility, built entirely from data the pipeline
already persists (scan_runs.metadata_json, historical_recommendations,
ingestion_log) — no new tables, no change to any qualification logic.

Added 2026-08-23 per operator directive: "I want better ongoing visibility
into the qualification funnel... for every production scan, persist or
summarize by league and market." The funnel itself was already computed
every scan (the PINNACLE_SUMMARY log line); this module makes it queryable
after the fact instead of only visible in scrolled-past logs, and adds the
EV-bucket/tier/exact-gate breakdown the operator asked for on top.

Read-only. Never changes a threshold, a gate, or a recommendation.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field


# EV-pct thresholds the operator wants bucketed (recommendations table
# stores ev_pct as a percentage, e.g. 3.0 for 3%).
EV_BUCKET_THRESHOLDS = (1.0, 2.0, 3.0, 5.0)

# Gate-label classification for historical_recommendations.disqualification_reasons
# — mirrors the exact gate order in src/official_picks.py::classify_recommendation,
# so a count here always traces back to one real gate, not a guess.
_GATE_PATTERNS: tuple[tuple[str, callable], ...] = (
    ("1_market_quality", lambda c: "Market quality" in c),
    ("1_no_settlement", lambda c: "settlement field" in c),
    ("2_stale_data", lambda c: c == "Data is stale"),
    ("3_game_status", lambda c: "not scheduled" in c),
    ("4_model_score", lambda c: "Model Score" in c),
    ("5_rec_status", lambda c: c.startswith("Status '")),
    ("6_book_count", lambda c: "contributing book" in c),
    ("7_mapping_confidence", lambda c: "Mapping confidence" in c),
    ("8_edge_threshold", lambda c: ("EV" in c or "Price Advantage" in c) and "<" in c),
    ("9_pinnacle_gate", lambda c: "Pinnacle approval" in c),
    ("10_ev_reliability", lambda c: "reliability gate" in c),
    ("11_identity_field", lambda c: c.startswith("Missing")),
    ("12_yn_reference_odds", lambda c: "YN reference odds" in c),
)


def _classify_gate(clause: str) -> str:
    for label, matches in _GATE_PATTERNS:
        if matches(clause):
            return label
    return f"other: {clause[:50]}"


@dataclass
class MarketFunnel:
    """Funnel counts for one (league, market_type) slice."""
    league: str
    market_type: str
    n_recommendations: int = 0
    n_positive_ev: int = 0
    n_ev_ge_1pct: int = 0
    n_ev_ge_2pct: int = 0
    n_ev_ge_3pct: int = 0
    n_ev_ge_5pct: int = 0
    n_model_score_pass: int = 0  # model_score >= official_min_model_score
    n_pinnacle_or_loo_valid: int = 0  # pinnacle_approved OR pinnacle_found is False (LOO-eligible)
    n_official: int = 0
    n_discovery: int = 0
    n_research: int = 0
    gate_rejections: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        return {
            "league": self.league,
            "market_type": self.market_type,
            "n_recommendations": self.n_recommendations,
            "n_positive_ev": self.n_positive_ev,
            "n_ev_ge_1pct": self.n_ev_ge_1pct,
            "n_ev_ge_2pct": self.n_ev_ge_2pct,
            "n_ev_ge_3pct": self.n_ev_ge_3pct,
            "n_ev_ge_5pct": self.n_ev_ge_5pct,
            "n_model_score_pass": self.n_model_score_pass,
            "n_pinnacle_or_loo_valid": self.n_pinnacle_or_loo_valid,
            "n_official": self.n_official,
            "n_discovery": self.n_discovery,
            "n_research": self.n_research,
            "gate_rejections": dict(self.gate_rejections),
        }


def _latest_scan_run_ids(conn, league: str | None, limit: int) -> list[str]:
    """The most recent distinct scan_run_id(s) with real recommendations,
    optionally restricted to one league."""
    if league:
        rows = conn.execute(
            "SELECT DISTINCT scan_run_id, MAX(created_at) AS latest "
            "FROM historical_recommendations WHERE league = ? AND scan_run_id IS NOT NULL "
            "GROUP BY scan_run_id ORDER BY latest DESC LIMIT ?",
            (league, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT scan_run_id, MAX(created_at) AS latest "
            "FROM historical_recommendations WHERE scan_run_id IS NOT NULL "
            "GROUP BY scan_run_id ORDER BY latest DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["scan_run_id"] for r in rows]


def get_scan_level_funnel(conn, scan_run_id: str) -> dict | None:
    """The scanner's own group-level funnel for one scan_run_id, as
    persisted into scan_runs.metadata_json at finish_run time (comparable
    market groups formed, insufficient-books/EV-threshold/no-edge
    rejections, Pinnacle match/missing/stale counts). Returns None if this
    scan_run predates the metadata being persisted (2026-08-23) or never
    reached the Pinnacle injection stage (e.g. zero games that day)."""
    row = conn.execute(
        "SELECT metadata_json FROM scan_runs WHERE run_id = ?", (scan_run_id,)
    ).fetchone()
    if not row or not row["metadata_json"]:
        return None
    try:
        meta = json.loads(row["metadata_json"])
    except (TypeError, ValueError):
        return None
    return meta.get("pinnacle_funnel")


def get_recommendation_level_funnel(
    conn, scan_run_id: str, *, official_min_model_score: float = 7.0,
) -> list[MarketFunnel]:
    """EV-bucket / model-score / Pinnacle-validity / tier / exact-gate
    breakdown for one scan_run_id, grouped by (league, market_type)."""
    rows = conn.execute(
        "SELECT league, market_type, ev_pct, model_score, recommendation_tier, "
        "pinnacle_approved, pinnacle_found, disqualification_reasons "
        "FROM historical_recommendations WHERE scan_run_id = ?",
        (scan_run_id,),
    ).fetchall()

    by_key: dict[tuple[str, str], MarketFunnel] = {}
    for r in rows:
        key = (r["league"] or "?", r["market_type"] or "?")
        mf = by_key.setdefault(key, MarketFunnel(league=key[0], market_type=key[1]))
        mf.n_recommendations += 1

        ev = r["ev_pct"]
        if ev is not None:
            if ev > 0:
                mf.n_positive_ev += 1
            for threshold, attr in zip(
                EV_BUCKET_THRESHOLDS,
                ("n_ev_ge_1pct", "n_ev_ge_2pct", "n_ev_ge_3pct", "n_ev_ge_5pct"),
            ):
                if ev >= threshold:
                    setattr(mf, attr, getattr(mf, attr) + 1)

        score = r["model_score"]
        if score is not None and score >= official_min_model_score:
            mf.n_model_score_pass += 1

        # "Pinnacle/LOO validation" = either Pinnacle explicitly approved
        # it, or Pinnacle genuinely has no data at this exact market
        # (the real Gate 9 LOO-fallback eligibility condition — not
        # simply "Pinnacle absent", which also covers the blocked
        # present-but-one-sided/mismatched case).
        pinnacle_found = r["pinnacle_found"]
        # SQLite stores this as an int 0/1, not a real Python bool, so
        # `is False` (an identity check) never matches there — only
        # PostgreSQL's native boolean columns would. Must be a value
        # check (explicitly known-false, not merely absent/None).
        genuinely_missing = pinnacle_found is not None and not pinnacle_found
        if r["pinnacle_approved"] or genuinely_missing:
            mf.n_pinnacle_or_loo_valid += 1

        tier = r["recommendation_tier"]
        if tier == "OFFICIAL_TRACKED":
            mf.n_official += 1
        elif tier == "DISCOVERY_TRACKED":
            mf.n_discovery += 1
        else:
            mf.n_research += 1

        for clause in (r["disqualification_reasons"] or "").split(";"):
            clause = clause.strip()
            if clause:
                mf.gate_rejections[_classify_gate(clause)] += 1

    return list(by_key.values())


def get_ingestion_funnel(conn, scan_run_id: str) -> dict | None:
    """Raw vs. approved/normalized odds-row counts for one scan_run_id, if
    available. Only populated for scans that went through
    daily_pipeline.py's own SportsGameOdds-specific ingest stage
    (ingestion_log is keyed by ingestion_run_id, a separate identity from
    scan_run_id) — WNBA's own provider path and the Odds-API 429 fallback
    path never populate it. Returns None (not zero) when unavailable, so
    callers report "not available for this run" rather than a misleading
    0/0."""
    rec = conn.execute(
        "SELECT ingestion_run_id FROM historical_recommendations "
        "WHERE scan_run_id = ? AND ingestion_run_id IS NOT NULL LIMIT 1",
        (scan_run_id,),
    ).fetchone()
    if not rec or not rec["ingestion_run_id"]:
        return None
    row = conn.execute(
        "SELECT SUM(odds_rows) AS approved, SUM(audit_rows) AS raw "
        "FROM ingestion_log WHERE run_id = ?",
        (rec["ingestion_run_id"],),
    ).fetchone()
    if not row or row["raw"] is None:
        return None
    return {"raw_odds_rows": row["raw"], "normalized_approved_rows": row["approved"]}


def build_funnel_report(conn, league: str | None = None, limit_runs: int = 1) -> dict:
    """Full funnel report for the most recent scan(s) — optionally scoped
    to one league. Returns a dict keyed by scan_run_id, each with the
    scan-level group funnel, the recommendation-level EV/tier/gate
    breakdown (per market_type), and ingestion-level raw/approved counts
    when available."""
    run_ids = _latest_scan_run_ids(conn, league, limit_runs)
    report: dict = {}
    for run_id in run_ids:
        scan_funnel = get_scan_level_funnel(conn, run_id)
        rec_funnels = get_recommendation_level_funnel(conn, run_id)
        ingestion_funnel = get_ingestion_funnel(conn, run_id)
        report[run_id] = {
            "scan_level": scan_funnel,
            "ingestion_level": ingestion_funnel,
            "by_market": [mf.to_dict() for mf in rec_funnels],
        }
    return report


def print_funnel_report(conn, league: str | None = None, limit_runs: int = 1) -> None:
    """Human-readable console summary — for ad-hoc checks, not the
    primary programmatic interface (use build_funnel_report for that)."""
    report = build_funnel_report(conn, league=league, limit_runs=limit_runs)
    if not report:
        print("No scan runs with recommendations found" +
              (f" for league={league}" if league else "") + ".")
        return
    for run_id, data in report.items():
        print(f"\n=== scan_run {run_id} ===")
        ing = data["ingestion_level"]
        if ing:
            print(f"  raw/normalized odds rows: "
                  f"{ing['raw_odds_rows']}/{ing['normalized_approved_rows']}")
        else:
            print("  raw/normalized odds rows: not available for this run")
        sl = data["scan_level"]
        if sl:
            print(f"  comparable market groups: {sl.get('total_groups', '?')}  "
                  f"pinnacle_exact_match={sl.get('pinnacle_exact_match', '?')}  "
                  f"pinnacle_missing={sl.get('pinnacle_missing', '?')}  "
                  f"insufficient_books={sl.get('insufficient_comparison_books', '?')}  "
                  f"stale_skipped={sl.get('pinnacle_stale_skipped', '?')}")
        else:
            print("  comparable market groups: not available for this run "
                  "(scan predates funnel persistence, or zero games that day)")
        for mf in data["by_market"]:
            print(f"  [{mf['league']}/{mf['market_type']}] "
                  f"recs={mf['n_recommendations']} pos_ev={mf['n_positive_ev']} "
                  f"ev>=1%={mf['n_ev_ge_1pct']} ev>=2%={mf['n_ev_ge_2pct']} "
                  f"ev>=3%={mf['n_ev_ge_3pct']} ev>=5%={mf['n_ev_ge_5pct']} "
                  f"model_score_pass={mf['n_model_score_pass']} "
                  f"pinnacle_or_loo_valid={mf['n_pinnacle_or_loo_valid']} "
                  f"official={mf['n_official']} discovery={mf['n_discovery']} "
                  f"research={mf['n_research']}")
            if mf["gate_rejections"]:
                top = sorted(mf["gate_rejections"].items(), key=lambda kv: -kv[1])
                print(f"    gate rejections: {top}")


if __name__ == "__main__":
    import argparse
    from database.db_manager import get_connection

    parser = argparse.ArgumentParser(description="Print the qualification funnel for recent scans.")
    parser.add_argument("--league", default=None, help="MLB, NFL, or WNBA (default: all)")
    parser.add_argument("--runs", type=int, default=1, help="How many recent scan runs to show")
    args = parser.parse_args()

    _conn = get_connection()
    try:
        print_funnel_report(_conn, league=args.league, limit_runs=args.runs)
    finally:
        _conn.close()
