"""Production audit trail for recommendation traceability.

Ensures every live recommendation can be traced through:
API request -> API response -> ingestion -> scan -> recommendation
snapshot -> confidence -> delivery decision -> delivery attempt ->
closing-price capture -> result record -> settlement -> analytics.

Provides a trace command and structured query for full lifecycle visibility.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TraceStep:
    """A single step in a recommendation's lifecycle."""
    step_id: str = ""
    recommendation_id: str = ""
    step_name: str = ""
    timestamp: str = ""
    status: str = ""  # ok, error, skipped
    details: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.details is None:
            self.details = {}
        if not self.step_id:
            self.step_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecommendationTrace:
    """Full trace of a recommendation's lifecycle."""
    recommendation_id: str = ""
    player_name: str = ""
    market_type: str = ""
    sportsbook: str = ""
    created_at: str = ""
    steps: list[TraceStep] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.steps is None:
            self.steps = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation_id,
            "player_name": self.player_name,
            "market_type": self.market_type,
            "sportsbook": self.sportsbook,
            "created_at": self.created_at,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_human_readable(self) -> str:
        """Format as human-readable trace."""
        lines = [
            f"Trace for recommendation {self.recommendation_id[:16]}",
            f"  Player: {self.player_name}",
            f"  Market: {self.market_type}",
            f"  Sportsbook: {self.sportsbook}",
            f"  Created: {self.created_at}",
            "",
        ]
        for i, step in enumerate(self.steps, 1):
            status_icon = {"ok": "OK", "error": "ERR", "skipped": "---"}.get(step.status, "?")
            lines.append(f"  [{i:2d}] {step.step_name} ({status_icon}) @ {step.timestamp}")
            if step.details:
                for k, v in step.details.items():
                    val_str = str(v)
                    if len(val_str) > 100:
                        val_str = val_str[:100] + "..."
                    lines.append(f"       {k}: {val_str}")
        return "\n".join(lines)


# ── Schema ─────────────────────────────────────────────────────────

TRACE_TABLE = "recommendation_traces"


def init_trace_table(conn: Any) -> None:
    """Create the recommendation_traces table if it doesn't exist."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRACE_TABLE} (
            trace_id TEXT PRIMARY KEY,
            recommendation_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ok',
            details_json TEXT DEFAULT '{{}}'
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{TRACE_TABLE}_rec_id
        ON {TRACE_TABLE} (recommendation_id)
    """)


def record_trace_step(conn: Any, step: TraceStep) -> None:
    """Persist a trace step."""
    conn.execute(f"""
        INSERT OR REPLACE INTO {TRACE_TABLE}
        (trace_id, recommendation_id, step_name, timestamp, status, details_json)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        step.step_id, step.recommendation_id, step.step_name,
        step.timestamp, step.status, json.dumps(step.details),
    ))
    conn.commit()


def record_trace_steps(conn: Any, steps: list[TraceStep]) -> None:
    """Persist multiple trace steps."""
    for step in steps:
        record_trace_step(conn, step)


def get_recommendation_trace(
    conn: Any,
    recommendation_id: str,
) -> RecommendationTrace | None:
    """Build a full trace for a recommendation.

    Returns None if the recommendation ID is not found.
    """
    # Get recommendation details
    try:
        rec = conn.execute("""
            SELECT recommendation_id, player_name, market_type, sportsbook, observation_timestamp
            FROM recommendations
            WHERE recommendation_id = ?
        """, (recommendation_id,)).fetchone()
    except Exception:
        rec = None

    # Also check historical_recommendations
    if rec is None:
        try:
            rec = conn.execute("""
                SELECT recommendation_id, player_name, market_type, sportsbook, observation_timestamp
                FROM historical_recommendations
                WHERE recommendation_id = ?
            """, (recommendation_id,)).fetchone()
        except Exception:
            rec = None

    if rec is None:
        return None

    # Get trace steps
    rows = conn.execute(f"""
        SELECT trace_id, recommendation_id, step_name, timestamp, status, details_json
        FROM {TRACE_TABLE}
        WHERE recommendation_id = ?
        ORDER BY timestamp ASC
    """, (recommendation_id,)).fetchall()

    steps = [
        TraceStep(
            step_id=r[0], recommendation_id=r[1], step_name=r[2],
            timestamp=r[3], status=r[4], details=json.loads(r[5] or "{}"),
        )
        for r in rows
    ]

    return RecommendationTrace(
        recommendation_id=rec[0],
        player_name=rec[1] or "",
        market_type=rec[2] or "",
        sportsbook=rec[3] or "",
        created_at=rec[4] or "",
        steps=steps,
    )


def redact_secrets(trace: RecommendationTrace) -> RecommendationTrace:
    """Redact any secret values from trace details."""
    import re
    secret_patterns = [
        re.compile(r"api[_-]?key", re.IGNORECASE),
        re.compile(r"webhook[_-]?url", re.IGNORECASE),
        re.compile(r"credential", re.IGNORECASE),
        re.compile(r"token", re.IGNORECASE),
        re.compile(r"password", re.IGNORECASE),
    ]
    for step in trace.steps:
        redacted_details = {}
        for k, v in step.details.items():
            val_str = str(v)
            is_secret = any(p.search(k) for p in secret_patterns)
            if is_secret and len(val_str) > 4:
                redacted_details[k] = val_str[:2] + "***" + val_str[-2:]
            else:
                redacted_details[k] = v
        step.details = redacted_details
    return trace


# ── Lifecycle step recorders ──────────────────────────────────────

def record_api_request(conn: Any, rec_id: str, endpoint: str, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="api_request",
                     status=status, details={"endpoint": endpoint, **details})
    record_trace_step(conn, step)
    return step


def record_ingestion(conn: Any, rec_id: str, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="ingestion",
                     status=status, details=details)
    record_trace_step(conn, step)
    return step


def record_scan(conn: Any, rec_id: str, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="scan",
                     status=status, details=details)
    record_trace_step(conn, step)
    return step


def record_recommendation(conn: Any, rec_id: str, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="recommendation",
                     status=status, details=details)
    record_trace_step(conn, step)
    return step


def record_confidence(conn: Any, rec_id: str, score: float, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="confidence",
                     status=status, details={"score": score, **details})
    record_trace_step(conn, step)
    return step


def record_delivery_decision(conn: Any, rec_id: str, decision: str, reason: str = "", status: str = "ok") -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="delivery_decision",
                     status=status, details={"decision": decision, "reason": reason})
    record_trace_step(conn, step)
    return step


def record_delivery_attempt(conn: Any, rec_id: str, channel: str, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="delivery_attempt",
                     status=status, details={"channel": channel, **details})
    record_trace_step(conn, step)
    return step


def record_closing_price(conn: Any, rec_id: str, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="closing_price",
                     status=status, details=details)
    record_trace_step(conn, step)
    return step


def record_settlement(conn: Any, rec_id: str, result: str, status: str = "ok", **details: Any) -> TraceStep:
    step = TraceStep(recommendation_id=rec_id, step_name="settlement",
                     status=status, details={"result": result, **details})
    record_trace_step(conn, step)
    return step
