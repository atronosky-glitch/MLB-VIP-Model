"""Worker runtime metrics: process memory, job->sport mapping, shared worker
state, and a dedicated heartbeat thread.

Why (2026-09-24 launch audit):
* The worker previously OOM-crashed on a 512MB plan and there was no way to
  tell WHICH job grew memory. Every job now logs RSS before/after.
* The heartbeat was written only between jobs, so one long scan made a
  healthy worker look dead to the health check. A daemon thread with its own
  short-lived DB connection now beats independently of job execution.
* The heartbeat row also carries the current job, last success and last
  failure so an operator can see "is the worker alive, what is it doing, what
  last failed" without reading raw tables.

Pure/portable: no third-party dependency (psutil is not installed).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def rss_mb() -> float | None:
    """Resident set size of this process in MB, or None if unavailable."""
    try:
        with open("/proc/self/status", encoding="ascii") as fh:      # Linux (Render)
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)   # kB -> MB
    except (OSError, ValueError, IndexError):
        pass
    try:                                                             # Windows (local dev)
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        fn = kernel32.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        if fn(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return round(counters.WorkingSetSize / (1024 * 1024), 1)
    except Exception:
        pass
    try:                                                             # other Unix
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except Exception:
        return None


_SPORT_BY_JOB_PREFIX = (
    ("wnba", "WNBA"), ("nfl", "NFL"), ("cfb", "NCAAF"), ("mlb", "MLB"),
)
_SPORT_BY_JOB = {
    "morning-run": "MLB", "pregame-check": "MLB",
    "morning-run-nfl": "NFL", "pregame-check-nfl": "NFL",
    "morning-run-cfb": "NCAAF", "pregame-check-cfb": "NCAAF",
}


def sport_for_job(job_type: str) -> str:
    """Sport a scheduled job belongs to ("ALL" for cross-sport jobs)."""
    if job_type in _SPORT_BY_JOB:
        return _SPORT_BY_JOB[job_type]
    for prefix, sport in _SPORT_BY_JOB_PREFIX:
        if job_type.startswith(prefix):
            return sport
    return "ALL"


def summarize_result(result: dict | None, max_items: int = 8) -> dict:
    """Small, log-safe view of a job result: only scalar numbers/strings under
    short keys (no nested payloads, no long strings)."""
    out: dict = {}
    for key, value in (result or {}).items():
        if len(out) >= max_items:
            break
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[str(key)] = value
        elif isinstance(value, str) and len(value) <= 60:
            out[str(key)] = value
    return out


@dataclass
class WorkerState:
    """Shared between the loop and the heartbeat thread."""
    current_job: str | None = None
    current_job_started_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start_job(self, job_type: str) -> None:
        with self._lock:
            self.current_job, self.current_job_started_at = job_type, time.time()

    def end_job(self) -> None:
        with self._lock:
            self.current_job = self.current_job_started_at = None

    def snapshot(self) -> tuple[str | None, float | None]:
        with self._lock:
            return self.current_job, self.current_job_started_at


WORKER_STATE = WorkerState()

HEARTBEAT_COLUMNS = [
    ("uptime_seconds", "REAL"), ("current_job", "TEXT"), ("current_job_seconds", "REAL"),
    ("rss_mb", "REAL"), ("last_success_at", "TEXT"), ("last_success_job", "TEXT"),
    ("last_failure_at", "TEXT"), ("last_failure_job", "TEXT"), ("last_failure_message", "TEXT"),
]


def _ensure_heartbeat_schema(conn) -> None:
    """Create/upgrade the heartbeat table. Called only when a write fails
    (missing table/columns), so steady-state beats issue no DDL."""
    from database.db_manager import _add_columns_if_missing
    conn.execute(
        "CREATE TABLE IF NOT EXISTS worker_heartbeat ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), last_heartbeat TEXT NOT NULL, worker_pid INTEGER)"
    )
    _add_columns_if_missing(conn, "worker_heartbeat", HEARTBEAT_COLUMNS)
    conn.commit()


_STARTED_AT = time.time()


def write_heartbeat(conn, state: WorkerState = WORKER_STATE) -> None:
    """Upsert the single heartbeat row (portable SQLite/PostgreSQL). Never raises."""
    job, started = state.snapshot()
    now = time.time()
    params = (datetime.now(timezone.utc).isoformat(), os.getpid(), round(now - _STARTED_AT, 1),
              job, round(now - started, 1) if started else None, rss_mb())
    sql = """INSERT INTO worker_heartbeat (id, last_heartbeat, worker_pid, uptime_seconds, current_job,
                                           current_job_seconds, rss_mb)
             VALUES (1, ?, ?, ?, ?, ?, ?)
             ON CONFLICT (id) DO UPDATE SET last_heartbeat = excluded.last_heartbeat,
                 worker_pid = excluded.worker_pid, uptime_seconds = excluded.uptime_seconds,
                 current_job = excluded.current_job, current_job_seconds = excluded.current_job_seconds,
                 rss_mb = excluded.rss_mb"""
    for attempt in (1, 2):
        try:
            conn.execute(sql, params)
            conn.commit()
            return
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt == 2:
                logger.warning("Failed to write heartbeat: %s", type(exc).__name__)
                return
            try:
                _ensure_heartbeat_schema(conn)
            except Exception:
                logger.warning("Failed to prepare the heartbeat table")
                return


def record_job_outcome(conn, job_type: str, ok: bool, message: str | None = None) -> None:
    """Record the last success / last failure on the heartbeat row. Never raises."""
    ts = datetime.now(timezone.utc).isoformat()

    def _do() -> None:
        # the row may not exist yet (first job before the first beat)
        conn.execute(
            "INSERT INTO worker_heartbeat (id, last_heartbeat, worker_pid) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO NOTHING", (ts, os.getpid()),
        )
        if ok:
            conn.execute("UPDATE worker_heartbeat SET last_success_at = ?, last_success_job = ? WHERE id = 1",
                         (ts, job_type))
        else:
            from src.failure_diagnostics import sanitize_message
            conn.execute(
                "UPDATE worker_heartbeat SET last_failure_at = ?, last_failure_job = ?, last_failure_message = ? "
                "WHERE id = 1", (ts, job_type, sanitize_message(message or "", 240)),
            )
        conn.commit()

    for attempt in (1, 2):
        try:
            _do()
            return
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt == 2:
                logger.warning("Failed to record job outcome: %s", type(exc).__name__)
                return
            try:
                _ensure_heartbeat_schema(conn)
            except Exception:
                return


class HeartbeatThread(threading.Thread):
    """Beats independently of job execution using its OWN short-lived DB
    connection per beat (a psycopg2 connection is not shared across threads),
    so a long-running scan cannot make a live worker look dead."""

    def __init__(self, connect, interval_seconds: float = 30.0, state: WorkerState = WORKER_STATE):
        super().__init__(name="worker-heartbeat", daemon=True)
        self._connect, self._interval, self._state = connect, interval_seconds, state
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            conn = None
            try:
                conn = self._connect()
                write_heartbeat(conn, self._state)
            except Exception as exc:
                logger.warning("Heartbeat thread could not beat: %s", type(exc).__name__)
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
            self._stop_event.wait(self._interval)

    def stop(self) -> None:
        self._stop_event.set()
