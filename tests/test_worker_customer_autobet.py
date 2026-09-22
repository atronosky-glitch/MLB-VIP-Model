"""Tests for the customer Auto-Bet job's wiring into the real
production worker (src/worker.py) -- proves a qualifying model
recommendation can actually reach src.execution.customer_autobet's
pipeline via `python -m src.worker`, not a separate demo path.

Does not re-test customer_autobet.py's own execution logic (see
tests/test_customer_autobet.py) -- only that the worker's job
dispatcher wires "customer-autobet-scan" to it, on a real interval, and
never crashes the worker loop if the pass itself fails."""

from __future__ import annotations

from unittest import mock

import src.worker as worker


class _FakeConfig:
    database_path = ":memory:"


class TestDispatchWiring:
    def test_customer_autobet_scan_is_registered_in_the_dispatch_table(self, db_conn):
        with mock.patch("src.execution.customer_autobet.run_customer_autobet_pass") as mocked:
            mocked.return_value = {"accounts_considered": 0, "executed": 0, "skipped": 0, "failed": 0}
            result = worker._execute_job("customer-autobet-scan", db_conn, _FakeConfig())
        mocked.assert_called_once()
        assert result["status"] == "success"

    def test_unknown_job_type_still_skips_cleanly(self, db_conn):
        result = worker._execute_job("not-a-real-job", db_conn, _FakeConfig())
        assert result["status"] == "skipped"


class TestRunCustomerAutobetScan:
    def test_calls_run_customer_autobet_pass_with_the_worker_config(self):
        config = _FakeConfig()
        with mock.patch(
            "src.execution.customer_autobet.run_customer_autobet_pass",
            return_value={"accounts_considered": 2, "executed": 1, "skipped": 1, "failed": 0},
        ) as mocked:
            result = worker._run_customer_autobet_scan(config)
        mocked.assert_called_once_with(config)
        assert result["status"] == "success"
        assert result["accounts_considered"] == 2
        assert result["executed"] == 1

    def test_an_exception_in_the_pass_is_caught_not_raised(self):
        """A broken customer Auto-Bet pass (e.g. a DB hiccup) must
        never crash the worker's whole job loop -- matches every other
        job handler's isolation in this file."""
        config = _FakeConfig()
        with mock.patch(
            "src.execution.customer_autobet.run_customer_autobet_pass",
            side_effect=RuntimeError("boom"),
        ):
            result = worker._run_customer_autobet_scan(config)
        assert result["status"] == "failed"


class TestSchedulingInterval:
    def test_interval_constant_exists_and_is_positive(self):
        assert worker.CUSTOMER_AUTOBET_SCAN_INTERVAL_MINUTES > 0

    def test_main_loop_schedules_the_job_on_interval(self):
        """Static-source check (matching this codebase's established
        convention for worker-loop wiring): the persistent loop
        actually queues 'customer-autobet-scan' gated by
        CUSTOMER_AUTOBET_SCAN_INTERVAL_MINUTES, the same pattern as the
        arbitrage/middle scan immediately above it."""
        import inspect
        source = inspect.getsource(worker.run_worker_persistent)
        assert '_create_job_if_not_queued(conn, "customer-autobet-scan")' in source
        assert "CUSTOMER_AUTOBET_SCAN_INTERVAL_MINUTES" in source
