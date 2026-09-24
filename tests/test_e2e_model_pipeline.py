"""END-TO-END model pipeline through PRODUCTION functions (external services
mocked): multi-book odds -> fair probability + EV (LOO / Pinnacle reference)
-> qualification -> persistence -> official pick -> customer retrieval ->
Discord alert exactly once (also after a "restart") -> final stat -> grading /
settlement -> units -> model performance.

The one piece of glue is turning the analysis output into the recommendation
dict (the daily pipeline's _stage_freeze does this with far more bookkeeping);
every function that DECIDES something is the real one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

import database.db_manager as dbm
from database.db_manager import (
    freeze_or_update_official_pick, get_settled_recommendations, init_db, save_player_stat_result,
    save_recommendation_result,
)
from src.automatic_grading import grade_available_recommendations
from src.grading import performance_summary
from src.official_picks import TIER_OFFICIAL, classify_recommendation
from src.player_prop_analysis import analyze_prop_group


def _dec(price: int) -> float:
    return price / 100 + 1 if price > 0 else 100 / -price + 1


def _group(dk_over=115, dk_under=-115, with_pinnacle=True):
    over = {f"book{i}": {"price": -110, "decimal_odds": _dec(-110), "line": 6.5} for i in range(3)}
    under = {f"book{i}": {"price": -110, "decimal_odds": _dec(-110), "line": 6.5} for i in range(3)}
    if with_pinnacle:
        over["Pinnacle"] = {"price": -110, "decimal_odds": _dec(-110), "line": 6.5}
        under["Pinnacle"] = {"price": -110, "decimal_odds": _dec(-110), "line": 6.5}
    over["DraftKings"] = {"price": dk_over, "decimal_odds": _dec(dk_over), "line": 6.5}
    under["DraftKings"] = {"price": dk_under, "decimal_odds": _dec(dk_under), "line": 6.5}
    return analyze_prop_group("EV1|P1|pitching_strikeouts_ou|ou|game|6.5", over, under, n_approved_rows=12)


def _recommendation(analysis: dict, *, start_hours=3) -> dict:
    best = analysis["best_ev"]
    now = datetime.now(timezone.utc)
    rec = {
        "event_id": "EV1", "player_id": "P1", "player_name": "Test Pitcher", "matchup": "Away Team @ Home Team",
        "market_type": "pitching_strikeouts_ou", "market_form": "ou", "period": "game", "line": 6.5,
        "side": best["side"], "sportsbook": best["sportsbook"], "offered_american_odds": best["american_odds"],
        "offered_decimal_odds": _dec(best["american_odds"]), "offered_implied_prob": 1 / _dec(best["american_odds"]),
        "fair_prob": analysis["nv_prob_over"], "ev_pct": best["ev_pct"], "n_consensus_books": analysis["n_paired_books"],
        "market_quality": analysis["market_quality"], "rec_status": best["bet_status"],
        "freshness_status": "FRESH", "event_status": "scheduled", "model_score": 8.5, "model_version": "v1",
        "pinnacle_approved": best.get("pinnacle_approved"), "scan_timestamp": now.isoformat(),
        "event_start_time": (now + timedelta(hours=start_hours)).isoformat(), "league": "MLB", "sport": "baseball",
    }
    return rec


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = str(tmp_path / "model.db")
    init_db(path)
    conn = dbm.get_connection(path)
    yield path, conn
    conn.close()


class TestFairProbabilityAndGating:
    def test_positive_edge_with_a_pinnacle_reference_is_official(self):
        result = _group()
        best = result["best_ev"]
        assert result["market_quality"] == "VALID_MARKET" and result["pinnacle_reference_used"] is True
        assert best["sportsbook"] == "DraftKings" and best["ev_pct"] == pytest.approx(7.5, abs=0.2)
        assert best["is_official"] is True and best["pinnacle_approved"] is True
        assert 0.49 < result["nv_prob_over"] < 0.51                                  # de-vigged fair prob ~50%

    def test_no_pinnacle_means_not_official_fail_closed(self):
        best = _group(with_pinnacle=False)["best_ev"]
        assert best is None or best["is_official"] is False

    def test_an_extreme_outlier_price_is_demoted_never_official(self):
        result = _group(dk_over=135, dk_under=-165)
        assert result["market_quality"] == "NEEDS_REVIEW"
        assert result["best_ev"]["is_official"] is False

    def test_break_even_and_negative_edges_are_never_recommended(self):
        for over in (-110, -115):
            best = _group(dk_over=over, dk_under=-110)["best_ev"]
            assert best is None or best["ev_pct"] <= 0 or best["is_official"] is False


class TestFullChain:
    def test_recommendation_to_customer_discord_settlement_and_stats(self, db):
        path, conn = db
        analysis = _group()
        rec = _recommendation(analysis)

        # qualification
        qualification = classify_recommendation(rec)
        assert qualification.tier == TIER_OFFICIAL, qualification.disqualification_reasons
        rec.update(qualification.to_dict())

        # persistence: idempotent
        first = save_recommendation_result(conn, rec)
        assert first.status == "saved"
        rec_id = first.recommendation_id
        assert save_recommendation_result(conn, rec).status == "duplicate"

        # official pick: frozen once, re-observation is a no-op
        rec["recommendation_id"] = rec_id
        assert freeze_or_update_official_pick(conn, rec)["action"] == "frozen"
        assert freeze_or_update_official_pick(conn, rec)["action"] == "duplicate"
        assert conn.execute("SELECT COUNT(*) FROM official_picks WHERE pick_status = 'ACTIVE'").fetchone()[0] == 1

        # every consumer sees exactly this official pick
        from src.discord_delivery import _load_actionable_recommendations, deliver_new_recommendation_alerts
        from src.execution.cli import _load_actionable_rows

        class _Cfg:
            database_path = path

            def execution_allowed_rec_statuses_list(self):
                return ["STRONG_EDGE", "POSITIVE_EDGE"]

        assert [r["recommendation_id"] for r in _load_actionable_recommendations(path)] == [rec_id]
        assert [r["recommendation_id"] for r in _load_actionable_rows(_Cfg(), official_only=True)] == [rec_id]

        # Discord: once, and still once after a "restart" (state lives in the DB)
        sent = []

        def fake_send(url, payload, *a, **k):
            sent.append(str(payload))
            return True

        hook = ["https://discord.com/api/webhooks/1/x"]
        with mock.patch("src.discord_delivery._send_webhook_raw", side_effect=fake_send):
            assert deliver_new_recommendation_alerts(path, hook)["sent"] == 1
            assert deliver_new_recommendation_alerts(path, hook)["sent"] == 0
            fresh = dbm.get_connection(path)                               # simulated process restart
            fresh.close()
            assert deliver_new_recommendation_alerts(path, hook)["sent"] == 0
        assert len(sent) == 1 and "Test Pitcher" in sent[0]

        # settlement: verified final stat -> grade -> units -> official pick outcome
        save_player_stat_result(conn, "EV1", "P1", "pitching_strikeouts_ou", final_stat_value=8,
                                result_source="verified-test", result_status="FINAL")
        assert grade_available_recommendations(conn)["graded"] == 1
        assert grade_available_recommendations(conn)["graded"] == 0          # idempotent
        settlement = conn.execute("SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
                                  (rec_id,)).fetchone()[0]
        assert settlement == "WIN"
        units = conn.execute("SELECT risk_units, profit_units FROM bet_units WHERE recommendation_id = ?",
                             (rec_id,)).fetchone()
        assert units[0] > 0 and units[1] == pytest.approx(units[0] * 1.15, rel=0.05)      # +115 win
        assert conn.execute("SELECT outcome FROM official_picks WHERE recommendation_id = ?", (rec_id,)).fetchone()[0] == "win"

        # model performance counts exactly this one official settled pick
        summary = performance_summary(get_settled_recommendations(conn))
        assert summary["wins"] == 1 and summary["losses"] == 0 and summary["roi"] > 0

    def test_a_loss_is_graded_correctly_and_not_counted_as_a_win(self, db):
        path, conn = db
        for suffix, final, expect in (("loss", 4, "LOSS"),):   # 4 strikeouts vs an Over 6.5
            analysis = _group()
            rec = _recommendation(analysis)
            rec["event_id"] = f"EV-{suffix}"
            rec["player_id"] = f"P-{suffix}"
            rec.update(classify_recommendation(rec).to_dict())
            rec_id = save_recommendation_result(conn, rec).recommendation_id
            rec["recommendation_id"] = rec_id
            freeze_or_update_official_pick(conn, rec)
            save_player_stat_result(conn, rec["event_id"], rec["player_id"], "pitching_strikeouts_ou",
                                    final_stat_value=final, result_source="verified-test", result_status="FINAL")
            grade_available_recommendations(conn)
            assert conn.execute("SELECT settlement_status FROM market_settlements WHERE recommendation_id = ?",
                                (rec_id,)).fetchone()[0] == expect
        summary = performance_summary(get_settled_recommendations(conn))
        assert summary["wins"] == 0 and summary["losses"] == 1

    def test_incomplete_data_never_settles(self, db):
        path, conn = db
        rec = _recommendation(_group())
        rec.update(classify_recommendation(rec).to_dict())
        rec_id = save_recommendation_result(conn, rec).recommendation_id
        rec["recommendation_id"] = rec_id
        freeze_or_update_official_pick(conn, rec)
        save_player_stat_result(conn, "EV1", "P1", "pitching_strikeouts_ou", final_stat_value=None,
                                result_source="verified-test", result_status="UNRESOLVED")
        assert grade_available_recommendations(conn)["graded"] == 0
        assert conn.execute("SELECT COUNT(*) FROM market_settlements WHERE recommendation_id = ?", (rec_id,)).fetchone()[0] == 0
