"""Global kill switch (persisted, survives process restart) and
per-provider error circuit breaker. Both are checked by
LiveExecutionService before every submission attempt; neither ever
cancels or closes existing positions automatically -- only new
submissions are blocked (sections 30-31).

This is deliberately separate from the Streamlit "LIVE MODE" toggle
(src/control_panel.py), which is an in-memory, per-session,
resets-to-OFF-on-restart ADDITIONAL gate living only in
st.session_state -- the two serve different purposes and neither
substitutes for the other.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def is_kill_switch_engaged(conn: Any) -> tuple[bool, str | None]:
    row = conn.execute("SELECT engaged, reason FROM live_kill_switch WHERE id = 1").fetchone()
    if row is None:
        return False, None
    return bool(row["engaged"]), row["reason"]


def engage_kill_switch(conn: Any, reason: str) -> None:
    conn.execute(
        "INSERT INTO live_kill_switch (id, engaged, engaged_at, reason) VALUES (1, 1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET engaged = 1, engaged_at = excluded.engaged_at, reason = excluded.reason",
        (datetime.now(timezone.utc).isoformat(), reason),
    )
    conn.commit()


def disengage_kill_switch(conn: Any) -> None:
    conn.execute(
        "INSERT INTO live_kill_switch (id, engaged, engaged_at, reason) VALUES (1, 0, NULL, NULL) "
        "ON CONFLICT(id) DO UPDATE SET engaged = 0, engaged_at = NULL, reason = NULL",
    )
    conn.commit()


def is_circuit_tripped(conn: Any, provider: str) -> bool:
    row = conn.execute(
        "SELECT tripped FROM live_provider_circuit_state WHERE provider = ?", (provider,)
    ).fetchone()
    return bool(row["tripped"]) if row is not None else False


def record_provider_error(conn: Any, provider: str, error_threshold: int, window_minutes: int) -> bool:
    """Records one live-submission error for *provider* and trips the
    circuit if *error_threshold* errors have occurred within
    *window_minutes*. Returns True if the circuit is now tripped."""
    now = datetime.now(timezone.utc)
    row = conn.execute(
        "SELECT consecutive_errors, window_start FROM live_provider_circuit_state WHERE provider = ?", (provider,)
    ).fetchone()

    if row is None or row["window_start"] is None:
        consecutive_errors, window_start = 1, now
    else:
        window_start = datetime.fromisoformat(row["window_start"])
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)
        if now - window_start > timedelta(minutes=window_minutes):
            consecutive_errors, window_start = 1, now
        else:
            consecutive_errors = row["consecutive_errors"] + 1

    tripped = consecutive_errors >= error_threshold
    conn.execute(
        "INSERT INTO live_provider_circuit_state (provider, consecutive_errors, window_start, tripped, tripped_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET consecutive_errors = excluded.consecutive_errors, "
        "window_start = excluded.window_start, tripped = excluded.tripped, "
        "tripped_at = COALESCE(live_provider_circuit_state.tripped_at, excluded.tripped_at), "
        "updated_at = excluded.updated_at",
        (
            provider, consecutive_errors, window_start.isoformat(), 1 if tripped else 0,
            now.isoformat() if tripped else None, now.isoformat(),
        ),
    )
    conn.commit()
    return tripped


def reset_provider_circuit(conn: Any, provider: str) -> None:
    """Called after a confirmed successful submission -- a healthy
    request resets the error streak for that provider."""
    conn.execute(
        "INSERT INTO live_provider_circuit_state (provider, consecutive_errors, window_start, tripped, tripped_at, updated_at) "
        "VALUES (?, 0, NULL, 0, NULL, ?) "
        "ON CONFLICT(provider) DO UPDATE SET consecutive_errors = 0, window_start = NULL, tripped = 0, "
        "tripped_at = NULL, updated_at = excluded.updated_at",
        (provider, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
