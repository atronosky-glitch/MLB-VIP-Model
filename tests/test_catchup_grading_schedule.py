"""Provider-independent grading trigger (launch audit P0, 2026-09-24): settlement
must not depend on the odds provider marking games final. Production evidence:
with SportsGameOdds out of quota no grading job was created for days and the
last settlement was ~46h old."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

import src.worker as worker
from src.automation import create_job, schedule_catchup_grading


def _rec(conn, *, started_hours_ago, settled=None, rec_id=None):
    rec_id = rec_id or f"r-{uuid.uuid4().hex[:8]}"
    start = (datetime.now(timezone.utc) - timedelta(hours=started_hours_ago)).isoformat()
    conn.execute(
        """INSERT INTO historical_recommendations (recommendation_id, fingerprint, event_id, player_id,
               market_type, market_form, period, side, sportsbook, offered_american_odds, offered_decimal_odds,
               offered_implied_prob, rec_status, scan_timestamp, event_start_time)
           VALUES (?, ?, 'E', 'P', 'game_moneyline', 'ml', 'full_game', 'AWAY', 'DK', 100, 2.0, 0.5,
                   'STRONG_EDGE', ?, ?)""",
        (rec_id, f"fp-{rec_id}", start, start),
    )
    if settled:
        conn.execute("INSERT INTO market_settlements (settlement_id, recommendation_id, settlement_status, settled_at) "
                     "VALUES (?, ?, ?, ?)", (str(uuid.uuid4()), rec_id, settled, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return rec_id


def _jobs(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM scheduled_jobs WHERE job_type = 'grading'").fetchall()]


def test_unsettled_game_that_started_hours_ago_queues_a_grading_job(db_conn):
    _rec(db_conn, started_hours_ago=6)
    job_id = schedule_catchup_grading(db_conn)
    assert job_id
    jobs = _jobs(db_conn)
    assert len(jobs) == 1 and jobs[0]["status"] == "pending" and jobs[0]["event_id"] is None


def test_works_with_no_games_table_rows_at_all(db_conn):
    # the failure mode: provider outage -> games table never shows 'final'
    assert db_conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    _rec(db_conn, started_hours_ago=5)
    assert schedule_catchup_grading(db_conn)


def test_nothing_to_settle_queues_nothing(db_conn):
    _rec(db_conn, started_hours_ago=1)                        # game still in progress / too recent
    _rec(db_conn, started_hours_ago=24 * 10)                  # too old to chase
    _rec(db_conn, started_hours_ago=8, settled="WIN")         # already settled
    assert schedule_catchup_grading(db_conn) is None
    assert _jobs(db_conn) == []


def test_unresolved_settlement_rows_still_count_as_work(db_conn):
    _rec(db_conn, started_hours_ago=8, settled="UNRESOLVED")
    assert schedule_catchup_grading(db_conn)


def test_never_stacks_jobs_or_hammers_providers(db_conn):
    _rec(db_conn, started_hours_ago=6)
    assert schedule_catchup_grading(db_conn)
    assert schedule_catchup_grading(db_conn) is None          # one already pending
    db_conn.execute("UPDATE scheduled_jobs SET status = 'completed' WHERE job_type = 'grading'")
    db_conn.commit()
    assert schedule_catchup_grading(db_conn) is None          # finished, but inside the min interval
    assert len(_jobs(db_conn)) == 1


def test_queues_again_once_the_interval_has_passed(db_conn):
    _rec(db_conn, started_hours_ago=6)
    job_id = schedule_catchup_grading(db_conn)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")    # SQLite-style timestamp
    db_conn.execute("UPDATE scheduled_jobs SET status = 'completed', created_at = ? WHERE job_id = ?", (old, job_id))
    db_conn.commit()
    assert schedule_catchup_grading(db_conn)
    assert len(_jobs(db_conn)) == 2


def test_worker_scheduling_check_uses_it(db_conn):
    _rec(db_conn, started_hours_ago=6)
    worker._check_and_schedule_grading(db_conn)
    assert len(_jobs(db_conn)) == 1
    worker._check_and_schedule_grading(db_conn)               # idempotent across loop iterations
    assert len(_jobs(db_conn)) == 1


def test_grading_job_is_dispatched_by_the_worker(db_conn):
    job = create_job(db_conn, "grading")
    with mock.patch.object(worker, "_run_grading", return_value={"status": "success"}) as run:
        assert worker._execute_job("grading", db_conn, mock.Mock())["status"] == "success"
    run.assert_called_once()
