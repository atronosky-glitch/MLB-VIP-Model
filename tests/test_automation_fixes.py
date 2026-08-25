"""Regression tests for automation fixes:

- pregame job-type consistency (``pregame`` vs ``pregame-check``)
- worker honoring ``scheduled_at`` when executing pending jobs
- maintenance cleanup of stale failed/pending/lock rows
- next-morning-run fallback in automation status
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import src.worker as worker
from src.automation import (
    create_job,
    get_automation_status,
    schedule_pregame_checks,
    trigger_pregame_refresh,
)


def test_worker_uses_shared_database_connection_layer():
    source = Path("src/worker.py").read_text(encoding="utf-8")
    assert "from database.db_manager import get_connection" in source
    assert "sqlite3.connect" not in source
    assert "sqlite3.Connection" not in source


def _job_types(conn) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT job_type, status FROM scheduled_jobs"
    ).fetchall()
    return [(r["job_type"], r["status"]) for r in rows]


class TestCatchupGradingLeagueDispatch:
    """_run_catchup_grading must route unresolved recs to the right
    per-league settlement module, and skip leagues with none (WNBA)."""

    def test_dispatches_by_league_and_skips_unavailable(self, monkeypatch):
        recs = [
            {"recommendation_id": "r-mlb", "league": "MLB"},
            {"recommendation_id": "r-nfl-1", "league": "NFL"},
            {"recommendation_id": "r-nfl-2", "league": "NFL"},
            {"recommendation_id": "r-legacy"},  # no league column populated yet
            {"recommendation_id": "r-wnba", "league": "WNBA"},
        ]
        calls = []

        def fake_mlb_ingest(conn, recommendations, client=None):
            calls.append(("MLB", [r["recommendation_id"] for r in recommendations]))
            return {"facts_saved": 1}

        def fake_nfl_ingest(conn, recommendations, client=None):
            calls.append(("NFL", [r["recommendation_id"] for r in recommendations]))
            return {"facts_saved": 2}

        def fake_wnba_ingest(conn, recommendations, client=None):
            calls.append(("WNBA", [r["recommendation_id"] for r in recommendations]))
            return {"facts_saved": 3}

        with patch("database.db_manager.get_unsettled_recommendations", return_value=recs), \
             patch("src.automatic_grading.grade_available_recommendations", return_value={"graded": 0}), \
             patch("src.automatic_grading.grade_available_game_recommendations", return_value={"graded": 0}), \
             patch("src.mlb_results.ingest_results_for_recommendations", side_effect=fake_mlb_ingest), \
             patch("src.nfl_results.ingest_results_for_recommendations", side_effect=fake_nfl_ingest), \
             patch("src.wnba_results.ingest_results_for_recommendations", side_effect=fake_wnba_ingest):
            result = worker._run_catchup_grading(conn=MagicMock())

        dispatched = dict(calls)
        assert set(dispatched["MLB"]) == {"r-mlb", "r-legacy"}  # legacy rows default to MLB
        assert set(dispatched["NFL"]) == {"r-nfl-1", "r-nfl-2"}
        assert set(dispatched["WNBA"]) == {"r-wnba"}
        assert result["results"]["MLB"]["facts_saved"] == 1
        assert result["results"]["NFL"]["facts_saved"] == 2
        assert result["results"]["WNBA"]["facts_saved"] == 3


class TestBackupSkipsUnderPostgres:
    """PostgreSQL production must never run the SQLite-only backup path."""

    def test_run_backup_skips_when_database_url_set(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db")
        with patch("src.backup_database.backup_database") as mock_backup:
            result = worker._run_backup(config=object())
        assert result == {"status": "skipped", "reason": "postgresql_managed_externally"}
        mock_backup.assert_not_called()

    def test_run_backup_runs_sqlite_backup_without_database_url(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        fake_config = type("Cfg", (), {
            "database_path": str(tmp_path / "db.sqlite"),
            "backup_retention_count": 7,
            "backup_compression": False,
        })()
        with patch("src.backup_database.backup_database", return_value=tmp_path / "backup.db") as mock_backup:
            result = worker._run_backup(fake_config)
        mock_backup.assert_called_once()
        assert result["status"] == "success"


class TestPregameJobTypeConsistency:
    """Pregame jobs must use the job type the worker dispatcher handles."""

    def test_schedule_pregame_checks_uses_pregame_check(self, db_conn):
        today = db_conn.execute("SELECT date('now')").fetchone()[0]
        fixed_now = datetime.fromisoformat(f"{today}T12:00:00+00:00")
        start_time = f"{today}T15:00:00+00:00"
        db_conn.execute(
            "INSERT INTO games (event_id, away_team, home_team, start_time, status) "
            "VALUES ('ev_pc', 'NYY', 'BOS', ?, 'scheduled')",
            (start_time,),
        )
        db_conn.commit()
        with patch("src.automation.datetime") as mock_datetime:
            mock_datetime.now.return_value = fixed_now
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            count = schedule_pregame_checks(db_conn)
        assert count == 1
        types = [t for t, _ in _job_types(db_conn)]
        assert "pregame-check" in types
        assert "pregame" not in types

    def test_trigger_pregame_refresh_uses_pregame_check(self, db_conn):
        job_id = trigger_pregame_refresh(db_conn, "ev1")
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["job_type"] == "pregame-check"

    def test_execute_pregame_check_dispatches(self, db_conn):
        with patch.object(worker, "_run_pregame_scan", return_value={"status": "success"}) as scan:
            result = worker._execute_job("pregame-check", db_conn, None)
        scan.assert_called_once_with(db_conn, None, None)
        assert result["status"] == "success"

    def test_legacy_pregame_type_no_longer_dispatched(self, db_conn):
        result = worker._execute_job("pregame", db_conn, None)
        assert result["status"] == "skipped"


class TestProcessPendingJobsDueTime:
    """Worker must not run jobs before their scheduled time."""

    def test_future_job_is_not_executed(self, db_conn):
        future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat()
        job_id = create_job(db_conn, "test-unknown", scheduled_at=future)
        executed = worker._process_pending_jobs(db_conn, None)
        assert executed == 0
        row = db_conn.execute(
            "SELECT status FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "pending"

    def test_due_job_is_executed(self, db_conn):
        past = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        job_id = create_job(db_conn, "test-unknown", scheduled_at=past)
        executed = worker._process_pending_jobs(db_conn, None)
        assert executed == 1
        row = db_conn.execute(
            "SELECT status FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "failed"  # unknown type -> marked failed

    def test_unparseable_schedule_is_treated_as_due(self, db_conn):
        job_id = create_job(db_conn, "test-unknown", scheduled_at="not-a-date")
        executed = worker._process_pending_jobs(db_conn, None)
        assert executed == 1
        row = db_conn.execute(
            "SELECT status FROM scheduled_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row["status"] == "failed"


class TestMaintenanceCleanup:
    """Daily cleanup bounds scheduled_jobs growth."""

    def test_deletes_old_failed_and_stale_pending(self, db_conn):
        old_failed = create_job(db_conn, "pregame-check")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'failed', completed_at = ? "
            "WHERE job_id = ?",
            ("2026-01-01T00:00:00+00:00", old_failed),
        )
        stale_pending = create_job(
            db_conn, "pregame-check", scheduled_at="2026-01-01T00:00:00+00:00"
        )
        recent_pending = create_job(
            db_conn,
            "pregame-check",
            scheduled_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )
        db_conn.commit()

        removed = worker._maintenance_cleanup(db_conn)
        assert removed >= 2

        rows = db_conn.execute("SELECT job_id FROM scheduled_jobs").fetchall()
        remaining = {r["job_id"] for r in rows}
        assert old_failed not in remaining
        assert stale_pending not in remaining
        assert recent_pending in remaining

    def test_keeps_recent_failed(self, db_conn):
        recent_failed = create_job(db_conn, "pregame-check")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'failed', completed_at = ? "
            "WHERE job_id = ?",
            (datetime.now(timezone.utc).isoformat(), recent_failed),
        )
        db_conn.commit()
        worker._maintenance_cleanup(db_conn)
        rows = db_conn.execute("SELECT job_id FROM scheduled_jobs").fetchall()
        assert recent_failed in {r["job_id"] for r in rows}


class TestPerLeagueJobLockIsolation:
    """A long-running pregame pipeline for one league must never block
    another league's pregame job — this was a real bug: the lock key used
    to be the single fixed string 'pregame-pipeline' for every league."""

    def test_mlb_and_nfl_pregame_locks_are_independent(self, db_conn):
        mlb_lock = worker._acquire_lock(db_conn, "pregame-pipeline-MLB")
        nfl_lock = worker._acquire_lock(db_conn, "pregame-pipeline-NFL")
        assert mlb_lock is not None
        assert nfl_lock is not None
        assert mlb_lock != nfl_lock

    def test_second_call_for_same_league_is_blocked(self, db_conn):
        first = worker._acquire_lock(db_conn, "pregame-pipeline-WNBA")
        second = worker._acquire_lock(db_conn, "pregame-pipeline-WNBA")
        assert first is not None
        assert second is None

    def test_run_pregame_scan_uses_league_scoped_lock(self, db_conn):
        with patch("src.daily_pipeline.run_pipeline", return_value=0):
            worker._run_pregame_scan(db_conn, MagicMock(output_dir="output"), league="NFL")
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE metadata = 'worker-lock'"
        ).fetchone()
        assert row["job_type"] == "pregame-pipeline-NFL"


class TestRunMorningScanExitCodeClassification:
    """EXIT_SUCCESS_NO_RECS (1) means the pipeline ran fine and simply
    found zero qualifying opportunities that day — a normal outcome for
    any league, not a failure. Every other job runner in worker.py already
    treated it this way; _run_morning_scan (used by both MLB's daily
    morning-run and NFL's morning-run-nfl) was the one holdout still
    treating any non-zero exit code as failed, which showed real "no
    picks today" runs as a red FAILED job on the Multi-League Health tab."""

    def test_no_recommendations_is_reported_as_success(self):
        with patch("src.daily_pipeline.run_pipeline", return_value=1):
            result = worker._run_morning_scan(MagicMock(output_dir="output"), league="NFL")
        assert result["status"] == "success"
        assert result["exit_code"] == 1

    def test_clean_success_is_still_success(self):
        with patch("src.daily_pipeline.run_pipeline", return_value=0):
            result = worker._run_morning_scan(MagicMock(output_dir="output"), league="MLB")
        assert result["status"] == "success"

    def test_real_failure_is_still_reported_as_failed(self):
        with patch("src.daily_pipeline.run_pipeline", return_value=3):
            result = worker._run_morning_scan(MagicMock(output_dir="output"), league="NFL")
        assert result["status"] == "failed"
        assert result["exit_code"] == 3


class TestMorningRunCatchUpWindow:
    """The narrow original 8:30-9:59 AM ET window meant a worker restart
    or redeploy landing anywhere in those 90 minutes silently skipped the
    entire day's morning run, with no catch-up — a real production gap
    (worker.py has had several redeploys land at arbitrary times this
    session). The existing-job-for-today check already makes it safe to
    call this any time of day; it should now actually do so."""

    def test_still_does_not_fire_before_830am(self, db_conn):
        now = worker._now_local().replace(hour=8, minute=0, second=0, microsecond=0)
        with patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_morning_run(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()["c"]
        assert count == 0

    def test_fires_in_original_window(self, db_conn):
        now = worker._now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        with patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_morning_run(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()
        assert row is not None

    def test_catches_up_later_in_the_day_if_missed(self, db_conn):
        # This is the exact scenario that used to lose an entire day:
        # nothing scheduled by 2 PM because the 8:30-9:59 window was
        # missed (e.g. worker was mid-redeploy) — it must still catch up
        # rather than silently waiting for tomorrow's window.
        now = worker._now_local().replace(hour=14, minute=0, second=0, microsecond=0)
        with patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_morning_run(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()
        assert row is not None

    def test_does_not_duplicate_if_already_scheduled_today(self, db_conn):
        morning = worker._now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        afternoon = morning.replace(hour=14)
        with patch.object(worker, "_now_local", return_value=morning):
            worker._check_and_schedule_morning_run(db_conn)
        with patch.object(worker, "_now_local", return_value=afternoon):
            worker._check_and_schedule_morning_run(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()["c"]
        assert count == 1

    def test_does_not_duplicate_across_the_utc_midnight_crossover(self, db_conn):
        """Regression: a real bug, not just a test artifact. The dedup
        check computes "today" from local (ET) time, but the job insert
        used to call an independent, unmocked datetime.now(timezone.utc)
        — during the ET-evening/UTC-midnight crossover window (~7-8 PM ET
        onward, well within this function's normal 8:30 AM-11:59 PM ET
        active window), those two clock reads can land on different
        calendar dates, breaking the dedup match and letting a real
        duplicate morning-run job get scheduled the same ET business day.
        Caught live 2026-08-23 when the real wall clock happened to be in
        this exact window."""
        morning = worker._now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        # 9 PM ET is 1-2 AM UTC the *next* calendar date (EDT/EST) — safely
        # inside the crossover window, same real ET calendar day as `morning`.
        evening = morning.replace(hour=21)
        assert evening.astimezone(timezone.utc).date() != morning.astimezone(timezone.utc).date(), (
            "test setup assumption broken: 9 PM ET should already be a "
            "different UTC calendar date than 9 AM ET"
        )
        with patch.object(worker, "_now_local", return_value=morning):
            worker._check_and_schedule_morning_run(db_conn)
        with patch.object(worker, "_now_local", return_value=evening):
            worker._check_and_schedule_morning_run(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()["c"]
        assert count == 1

    def _insert_finished_job(self, db_conn, status: str, scheduled_at, completed_at):
        """Directly insert a scheduled_jobs row with an explicit
        completed_at, rather than going through update_job_status (which
        stamps completed_at via SQL's real-wall-clock datetime('now') —
        useless for controlling elapsed time in a test)."""
        import uuid
        db_conn.execute(
            "INSERT INTO scheduled_jobs (job_id, job_type, status, scheduled_at, completed_at) "
            "VALUES (?, 'morning-run', ?, ?, ?)",
            (str(uuid.uuid4()), status, scheduled_at.isoformat(), completed_at.isoformat()),
        )
        db_conn.commit()

    def test_recently_failed_job_does_not_retry_within_cooldown(self, db_conn):
        # The real production scenario this closes: morning-run fired on
        # time, hit a transient API failure (exit code 3) and completed
        # (failed) in ~8 seconds — a failed job used to count as "already
        # ran today" exactly like a successful one, silently losing the
        # rest of the day. It must not retry instantly on every 60s tick
        # either, though — that would just hammer a possibly-still-down API.
        morning = worker._now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        self._insert_finished_job(db_conn, "failed", morning, morning)
        soon_after = morning.replace(minute=1)  # 1 minute later — well inside cooldown
        with patch.object(worker, "_now_local", return_value=soon_after):
            worker._check_and_schedule_morning_run(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()["c"]
        assert count == 1  # no retry yet — still within cooldown

    def test_failed_job_retries_after_cooldown_elapses(self, db_conn):
        morning = worker._now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        self._insert_finished_job(db_conn, "failed", morning, morning)
        later = morning.replace(hour=11)  # 2h later, past the 60-min cooldown
        with patch.object(worker, "_now_local", return_value=later):
            worker._check_and_schedule_morning_run(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()["c"]
        assert count == 2  # the original failed attempt + one retry

    def test_completed_job_never_retries_regardless_of_time(self, db_conn):
        morning = worker._now_local().replace(hour=9, minute=0, second=0, microsecond=0)
        self._insert_finished_job(db_conn, "completed", morning, morning)
        later = morning.replace(hour=20)
        with patch.object(worker, "_now_local", return_value=later):
            worker._check_and_schedule_morning_run(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'morning-run'"
        ).fetchone()["c"]
        assert count == 1


class TestCatchupGradingResilience:
    """One league's result-ingestion failure must not prevent grading for
    the other two leagues, or for the game-market grading pass."""

    def test_one_league_ingest_exception_does_not_block_others_or_grading(self):
        recs = [
            {"recommendation_id": "r-mlb", "league": "MLB"},
            {"recommendation_id": "r-wnba", "league": "WNBA"},
        ]

        def fake_mlb_ingest(conn, recommendations, client=None):
            return {"facts_saved": 1}

        def fake_wnba_ingest(conn, recommendations, client=None):
            raise RuntimeError("WNBA API exhausted / credits gone")

        with patch("database.db_manager.get_unsettled_recommendations", return_value=recs), \
             patch("src.automatic_grading.grade_available_recommendations",
                   return_value={"graded": 2}) as grade_mock, \
             patch("src.automatic_grading.grade_available_game_recommendations",
                   return_value={"graded": 1}) as game_grade_mock, \
             patch("src.mlb_results.ingest_results_for_recommendations", side_effect=fake_mlb_ingest), \
             patch("src.wnba_results.ingest_results_for_recommendations", side_effect=fake_wnba_ingest):
            result = worker._run_catchup_grading(conn=MagicMock())

        assert result["results"]["MLB"]["facts_saved"] == 1
        assert result["results"]["WNBA"]["error"] is True
        # Grading passes (which cover ALL leagues) still ran despite WNBA's failure.
        grade_mock.assert_called_once()
        game_grade_mock.assert_called_once()
        assert result["grading"]["graded"] == 2
        assert result["game_grading"]["graded"] == 1


class TestNFLWNBASchedulingChecks:
    """src.worker._check_and_schedule_nfl / _check_and_schedule_wnba —
    the glue between src/league_schedule.py's pure decisions and the
    scheduled_jobs queue."""

    def test_no_nfl_games_creates_no_jobs(self, db_conn):
        with patch.object(worker, "_discover_nfl_game_times", return_value=[]):
            worker._check_and_schedule_nfl(db_conn)
        count = db_conn.execute("SELECT COUNT(*) AS c FROM scheduled_jobs").fetchone()["c"]
        assert count == 0

    def test_nfl_game_day_creates_daily_scan_job(self, db_conn):
        now = worker._now_local()
        kickoff = now.replace(hour=20, minute=0, second=0, microsecond=0)
        with patch.object(worker, "_discover_nfl_game_times", return_value=[kickoff]), \
             patch.object(worker, "_now_local", return_value=now.replace(hour=9)):
            worker._check_and_schedule_nfl(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'morning-run-nfl'"
        ).fetchone()
        assert row is not None

    def test_nfl_scheduling_does_not_duplicate_queued_job(self, db_conn):
        now = worker._now_local().replace(hour=9)
        kickoff = now.replace(hour=20)
        with patch.object(worker, "_discover_nfl_game_times", return_value=[kickoff]), \
             patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_nfl(db_conn)
            worker._check_and_schedule_nfl(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'morning-run-nfl'"
        ).fetchone()["c"]
        assert count == 1

    def test_no_wnba_key_or_games_creates_no_jobs(self, db_conn):
        with patch.object(worker, "_discover_wnba_game_times", return_value=[]):
            worker._check_and_schedule_wnba(db_conn)
        count = db_conn.execute("SELECT COUNT(*) AS c FROM scheduled_jobs").fetchone()["c"]
        assert count == 0

    def test_wnba_game_today_schedules_odds_scan(self, db_conn):
        # Fixed midday "now" so tipoff (+6h) never crosses into the next
        # calendar day regardless of wall-clock time the suite runs at —
        # wnba_has_games_today is a same-local-date check, so a real
        # `_now_local()` near end-of-day made this test flaky.
        now = worker._now_local().replace(hour=12, minute=0, second=0, microsecond=0)
        tipoff = now + timedelta(hours=6)
        with patch.object(worker, "_discover_wnba_game_times", return_value=[tipoff]), \
             patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_wnba(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'wnba-odds-scan'"
        ).fetchone()
        assert row is not None

    def test_wnba_props_not_scheduled_when_credits_low(self, db_conn):
        from src.odds_api_credits import record_credit_usage
        now = worker._now_local().replace(hour=12, minute=0, second=0, microsecond=0)
        tipoff = now + timedelta(minutes=60)  # inside the props window
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=10)
        with patch.object(worker, "_discover_wnba_game_times", return_value=[tipoff]), \
             patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_wnba(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'wnba-props-scan'"
        ).fetchone()
        assert row is None

    def test_wnba_props_scheduled_near_tipoff_with_budget(self, db_conn):
        from src.odds_api_credits import record_credit_usage
        now = worker._now_local().replace(hour=12, minute=0, second=0, microsecond=0)
        tipoff = now + timedelta(minutes=60)
        # Above the default reserve (10% of DEFAULT_MONTHLY_BUDGET, which
        # is 2000 at the real current 20,000/mo tier) so this exercises
        # the "budget available" path, not the reserve-blocked one.
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=5000)
        with patch.object(worker, "_discover_wnba_game_times", return_value=[tipoff]), \
             patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_wnba(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'wnba-props-scan'"
        ).fetchone()
        assert row is not None


class TestMLBAndNFLPropsScheduling:
    """src.worker._check_and_schedule_mlb_props / the NFL props check
    folded into _check_and_schedule_nfl — added 2026-08-22 alongside
    MLB/NFL's new supplemental Odds-API props source."""

    def test_mlb_no_games_creates_no_props_job(self, db_conn):
        with patch.object(worker, "_mlb_game_times_from_db", return_value=[]):
            worker._check_and_schedule_mlb_props(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'mlb-props-scan'"
        ).fetchone()["c"]
        assert count == 0

    def test_mlb_game_inside_window_schedules_props_job(self, db_conn):
        now = worker._now_local().replace(hour=12, minute=0, second=0, microsecond=0)
        game_time = now + timedelta(hours=2)  # inside MLB's 3h props window
        with patch.object(worker, "_mlb_game_times_from_db", return_value=[game_time]), \
             patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_mlb_props(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'mlb-props-scan'"
        ).fetchone()
        assert row is not None

    def test_mlb_props_scheduling_does_not_duplicate_queued_job(self, db_conn):
        now = worker._now_local().replace(hour=12, minute=0, second=0, microsecond=0)
        game_time = now + timedelta(hours=2)
        with patch.object(worker, "_mlb_game_times_from_db", return_value=[game_time]), \
             patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_mlb_props(db_conn)
            worker._check_and_schedule_mlb_props(db_conn)
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM scheduled_jobs WHERE job_type = 'mlb-props-scan'"
        ).fetchone()["c"]
        assert count == 1

    def test_mlb_game_times_from_db_reads_the_games_table(self, db_conn):
        """Unlike NFL/WNBA, MLB's props scheduling must not make a new
        live API call — it reads the local games table, already
        populated by the daily SportsGameOdds morning run."""
        from datetime import datetime, timezone, timedelta as _td
        future = (datetime.now(timezone.utc) + _td(hours=3)).isoformat()
        db_conn.execute(
            """INSERT INTO games (event_id, league, away_team, home_team, start_time, status)
               VALUES ('g1', 'MLB', 'Away Team', 'Home Team', ?, 'scheduled')""",
            (future,),
        )
        db_conn.commit()
        times = worker._mlb_game_times_from_db(db_conn)
        assert len(times) == 1

    def test_mlb_game_times_from_db_ignores_other_leagues(self, db_conn):
        from datetime import datetime, timezone, timedelta as _td
        future = (datetime.now(timezone.utc) + _td(hours=3)).isoformat()
        db_conn.execute(
            """INSERT INTO games (event_id, league, away_team, home_team, start_time, status)
               VALUES ('g1', 'NFL', 'Away Team', 'Home Team', ?, 'scheduled')""",
            (future,),
        )
        db_conn.commit()
        times = worker._mlb_game_times_from_db(db_conn)
        assert times == []

    def test_nfl_props_job_scheduled_alongside_daily_scan(self, db_conn):
        """_check_and_schedule_nfl now also schedules nfl-props-scan,
        reusing the same discovered kickoff times as the primary scan
        decision (no extra API call)."""
        now = worker._now_local().replace(hour=15, minute=0, second=0, microsecond=0)
        kickoff = now + timedelta(hours=2)  # inside NFL's 4h props window
        with patch.object(worker, "_discover_nfl_game_times", return_value=[kickoff]), \
             patch.object(worker, "_now_local", return_value=now):
            worker._check_and_schedule_nfl(db_conn)
        row = db_conn.execute(
            "SELECT job_type FROM scheduled_jobs WHERE job_type = 'nfl-props-scan'"
        ).fetchone()
        assert row is not None

    def test_nfl_props_job_type_registered_in_dispatch(self):
        """mlb-props-scan / nfl-props-scan must actually route to their
        job runners, not silently no-op as an unknown job type."""
        import inspect
        source = inspect.getsource(worker._execute_job)
        assert '"mlb-props-scan"' in source
        assert '"nfl-props-scan"' in source


class TestWNBADiscoveryCacheTTL:
    """Real bug fix, 2026-08-22: _discover_wnba_game_times() calls
    get_events(), which takes no time-varying params -- an OddsAPIClient
    built with no max_cache_age would serve the same frozen event list
    forever after the first real call (reproduced locally: a 2-day-old
    cache file was still being served unconditionally). The client must
    be constructed with a bounded TTL, not the default."""

    def test_discover_wnba_uses_bounded_cache_ttl(self):
        from src.odds_api_client import EVENTS_CACHE_TTL_SECONDS

        fake_client = MagicMock()
        fake_client.get_events.return_value = ([], False)
        with patch("src.odds_api_client.OddsAPIClient", return_value=fake_client) as MockClient:
            worker._discover_wnba_game_times()

        _, kwargs = MockClient.call_args
        assert kwargs.get("max_cache_age") == EVENTS_CACHE_TTL_SECONDS


class TestNFLScheduleDiscoveryFallback:
    """Real bug: _discover_nfl_game_times() used to swallow EVERY
    exception (including a genuine SportsGameOdds 429) and return [],
    which means _check_and_schedule_nfl never queues a job at all --
    NFL scheduling went silent for days in production because of this.
    On a real 429 specifically, it must now fall back to The Odds API's
    free /events endpoint (the same already-integrated provider
    src/sports/nfl.py's game-odds quota fallback already uses), mirroring
    the existing "except requests.exceptions.HTTPError, check status ==
    429" pattern from src/daily_pipeline.py. Any other failure still
    degrades to []."""

    @staticmethod
    def _http_429_error():
        resp = MagicMock()
        resp.status_code = 429
        err = requests.exceptions.HTTPError("429 rate limit exceeded")
        err.response = resp
        return err

    @staticmethod
    def _http_500_error():
        resp = MagicMock()
        resp.status_code = 500
        err = requests.exceptions.HTTPError("500 server error")
        err.response = resp
        return err

    def test_sgo_healthy_uses_normal_path_no_fallback(self):
        kickoff = "2026-09-10T17:00:00Z"
        fake_sgo = MagicMock()
        fake_sgo.get_events.return_value = ({"data": [{"status": {"startsAt": kickoff}}]}, False)
        with patch("src.api_client.SportsGameOddsClient", return_value=fake_sgo), \
             patch("src.odds_api_client.OddsAPIClient") as MockOddsAPI:
            result = worker._discover_nfl_game_times()

        assert len(result) == 1
        MockOddsAPI.assert_not_called()

    def test_sgo_429_falls_back_to_odds_api(self):
        fake_sgo = MagicMock()
        fake_sgo.get_events.side_effect = self._http_429_error()
        fb_kickoff = "2026-09-10T17:00:00Z"
        fake_odds_api = MagicMock()
        fake_odds_api.get_events.return_value = ([{"commence_time": fb_kickoff}], False)

        with patch("src.api_client.SportsGameOddsClient", return_value=fake_sgo), \
             patch("src.odds_api_client.OddsAPIClient", return_value=fake_odds_api) as MockOddsAPI:
            result = worker._discover_nfl_game_times()

        assert len(result) == 1
        from src.sports.nfl import ODDS_API_SPORT_KEY
        _, kwargs = fake_odds_api.get_events.call_args
        assert kwargs.get("sport_key") == ODDS_API_SPORT_KEY
        MockOddsAPI.assert_called_once()

    def test_sgo_non_429_http_error_does_not_fall_back(self):
        fake_sgo = MagicMock()
        fake_sgo.get_events.side_effect = self._http_500_error()
        with patch("src.api_client.SportsGameOddsClient", return_value=fake_sgo), \
             patch("src.odds_api_client.OddsAPIClient") as MockOddsAPI:
            result = worker._discover_nfl_game_times()

        assert result == []
        MockOddsAPI.assert_not_called()

    def test_odds_api_fallback_also_failing_returns_empty_not_raise(self):
        fake_sgo = MagicMock()
        fake_sgo.get_events.side_effect = self._http_429_error()
        with patch("src.api_client.SportsGameOddsClient", return_value=fake_sgo), \
             patch("src.odds_api_client.OddsAPIClient", side_effect=RuntimeError("boom")):
            result = worker._discover_nfl_game_times()

        assert result == []

    def test_fallback_does_not_duplicate_or_invent_games(self):
        """The fallback returns exactly the real events The Odds API
        reports -- no merging with a partial/stale SGO result, no
        synthesized entries."""
        fake_sgo = MagicMock()
        fake_sgo.get_events.side_effect = self._http_429_error()
        real_times = ["2026-09-10T17:00:00Z", "2026-09-14T20:00:00Z"]
        fake_odds_api = MagicMock()
        fake_odds_api.get_events.return_value = (
            [{"commence_time": t} for t in real_times], False,
        )
        with patch("src.api_client.SportsGameOddsClient", return_value=fake_sgo), \
             patch("src.odds_api_client.OddsAPIClient", return_value=fake_odds_api):
            result = worker._discover_nfl_game_times()

        assert len(result) == len(real_times)
        assert len(result) == len(set(result))


class TestLastCompletedJobAt:
    def test_none_when_never_run(self, db_conn):
        assert worker._get_last_completed_job_at(db_conn, "wnba-odds-scan") is None

    def test_returns_completion_time_of_completed_job(self, db_conn):
        job_id = create_job(db_conn, "wnba-odds-scan")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'completed', completed_at = ? WHERE job_id = ?",
            ("2026-08-19T22:00:00+00:00", job_id),
        )
        db_conn.commit()
        result = worker._get_last_completed_job_at(db_conn, "wnba-odds-scan")
        assert result is not None
        assert result.year == 2026

    def test_ignores_pending_and_failed_jobs(self, db_conn):
        create_job(db_conn, "wnba-odds-scan")  # stays pending
        assert worker._get_last_completed_job_at(db_conn, "wnba-odds-scan") is None


class TestCreateJobIfNotQueued:
    def test_creates_when_none_pending(self, db_conn):
        job_id = worker._create_job_if_not_queued(db_conn, "wnba-odds-scan")
        assert job_id is not None

    def test_skips_when_already_pending(self, db_conn):
        first = worker._create_job_if_not_queued(db_conn, "wnba-odds-scan")
        second = worker._create_job_if_not_queued(db_conn, "wnba-odds-scan")
        assert first is not None
        assert second is None

    def test_creates_again_once_prior_completed(self, db_conn):
        first = worker._create_job_if_not_queued(db_conn, "wnba-odds-scan")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'completed' WHERE job_id = ?", (first,),
        )
        db_conn.commit()
        second = worker._create_job_if_not_queued(db_conn, "wnba-odds-scan")
        assert second is not None
        assert second != first


class TestAutomationStatusNextMorning:
    """Dashboard shows a future next-morning-run time."""

    def test_next_morning_fallback_is_future(self, db_conn):
        status = get_automation_status(db_conn)
        assert status["next_morning_run"] is not None
        dt = datetime.fromisoformat(status["next_morning_run"].replace("Z", "+00:00"))
        assert dt > datetime.now(timezone.utc)

    def test_next_morning_prefers_pending_job(self, db_conn):
        scheduled = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
        create_job(db_conn, "morning-run", scheduled_at=scheduled)
        status = get_automation_status(db_conn)
        assert status["next_morning_run"] == scheduled
