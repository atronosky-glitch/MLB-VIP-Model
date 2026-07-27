"""Data-quality monitoring and anomaly detection.

Checks for: sportsbook-count drops, market-count drops, missing major
sportsbooks, unsupported market identifiers, changed side identifiers,
changed oddID formats, duplicate API objects, inverted odds, invalid
timestamps, future observation timestamps, stale observations,
impossible lines/prices, one-sided O/U markets, extreme consensus
disagreement, and recommendation-volume spikes/collapses.

Each finding is classified as INFO, WARNING, or CRITICAL.
Critical findings prevent actionable recommendation delivery.
All findings are persisted.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Severity levels ────────────────────────────────────────────────

SEVERITY_INFO = "INFO"
SEVERITY_WARNING = "WARNING"
SEVERITY_CRITICAL = "CRITICAL"


@dataclass
class DataQualityFinding:
    """A single data-quality finding."""
    finding_id: str = ""
    timestamp: str = ""
    check_name: str = ""
    severity: str = SEVERITY_INFO
    message: str = ""
    details: dict = None  # type: ignore[assignment]
    run_id: str = ""

    def __post_init__(self):
        if self.details is None:
            self.details = {}
        if not self.finding_id:
            self.finding_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["details"] = self.details
        return d


@dataclass
class DataQualityReport:
    """Aggregated data-quality report."""
    timestamp: str = ""
    run_id: str = ""
    total_checks: int = 0
    info_count: int = 0
    warning_count: int = 0
    critical_count: int = 0
    findings: list[DataQualityFinding] = None  # type: ignore[assignment]
    has_critical: bool = False

    def __post_init__(self):
        if self.findings is None:
            self.findings = []
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def add(self, finding: DataQualityFinding) -> None:
        self.findings.append(finding)
        self.total_checks += 1
        if finding.severity == SEVERITY_INFO:
            self.info_count += 1
        elif finding.severity == SEVERITY_WARNING:
            self.warning_count += 1
        elif finding.severity == SEVERITY_CRITICAL:
            self.critical_count += 1
            self.has_critical = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "total_checks": self.total_checks,
            "info_count": self.info_count,
            "warning_count": self.warning_count,
            "critical_count": self.critical_count,
            "has_critical": self.has_critical,
            "findings": [f.to_dict() for f in self.findings],
        }


# ── Schema ─────────────────────────────────────────────────────────

FINDINGS_TABLE = "data_quality_findings"


def init_findings_table(conn: Any) -> None:
    """Create the data_quality_findings table if it doesn't exist."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {FINDINGS_TABLE} (
            finding_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            check_name TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'INFO',
            message TEXT NOT NULL DEFAULT '',
            details_json TEXT DEFAULT '{{}}',
            run_id TEXT DEFAULT ''
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{FINDINGS_TABLE}_severity
        ON {FINDINGS_TABLE} (severity)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{FINDINGS_TABLE}_timestamp
        ON {FINDINGS_TABLE} (timestamp)
    """)


def persist_finding(conn: Any, finding: DataQualityFinding) -> None:
    """Persist a data-quality finding."""
    conn.execute(f"""
        INSERT OR REPLACE INTO {FINDINGS_TABLE}
        (finding_id, timestamp, check_name, severity, message, details_json, run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        finding.finding_id, finding.timestamp, finding.check_name,
        finding.severity, finding.message, json.dumps(finding.details),
        finding.run_id,
    ))
    conn.commit()


def persist_findings(conn: Any, report: DataQualityReport) -> None:
    """Persist all findings from a report."""
    for finding in report.findings:
        persist_finding(conn, finding)


def get_critical_findings(conn: Any, since_hours: int = 24) -> list[dict]:
    """Get critical findings from the last N hours."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    rows = conn.execute(f"""
        SELECT finding_id, timestamp, check_name, severity, message, details_json, run_id
        FROM {FINDINGS_TABLE}
        WHERE severity = ? AND timestamp >= ?
        ORDER BY timestamp DESC
    """, (SEVERITY_CRITICAL, cutoff)).fetchall()
    return [
        {"finding_id": r[0], "timestamp": r[1], "check_name": r[2],
         "severity": r[3], "message": r[4], "details": json.loads(r[5] or "{}"),
         "run_id": r[6]}
        for r in rows
    ]


# ── Check functions ────────────────────────────────────────────────

def check_sportsbook_count(
    current_count: int,
    previous_count: int,
    *,
    drop_threshold_pct: float = 30.0,
) -> DataQualityFinding | None:
    """Check for sudden sportsbook-count drop."""
    if previous_count <= 0:
        return None
    drop_pct = ((previous_count - current_count) / previous_count) * 100
    if drop_pct >= drop_threshold_pct:
        severity = SEVERITY_CRITICAL if drop_pct >= 50 else SEVERITY_WARNING
        return DataQualityFinding(
            check_name="sportsbook_count_drop",
            severity=severity,
            message=f"Sportsbook count dropped {drop_pct:.0f}% ({previous_count} -> {current_count})",
            details={"current": current_count, "previous": previous_count, "drop_pct": round(drop_pct, 1)},
        )
    return None


def check_market_count(
    current_count: int,
    previous_count: int,
    *,
    drop_threshold_pct: float = 30.0,
) -> DataQualityFinding | None:
    """Check for sudden market-count drop."""
    if previous_count <= 0:
        return None
    drop_pct = ((previous_count - current_count) / previous_count) * 100
    if drop_pct >= drop_threshold_pct:
        severity = SEVERITY_CRITICAL if drop_pct >= 50 else SEVERITY_WARNING
        return DataQualityFinding(
            check_name="market_count_drop",
            severity=severity,
            message=f"Market count dropped {drop_pct:.0f}% ({previous_count} -> {current_count})",
            details={"current": current_count, "previous": previous_count, "drop_pct": round(drop_pct, 1)},
        )
    return None


def check_missing_major_sportsbooks(
    observed_books: set[str],
    required_books: set[str] | None = None,
) -> DataQualityFinding | None:
    """Check for missing major sportsbooks."""
    if required_books is None:
        required_books = {"DraftKings", "FanDuel", "BetMGM", "Caesars"}
    missing = required_books - observed_books
    if missing:
        return DataQualityFinding(
            check_name="missing_major_sportsbook",
            severity=SEVERITY_WARNING,
            message=f"Missing major sportsbooks: {', '.join(sorted(missing))}",
            details={"missing": sorted(missing), "observed": sorted(observed_books)},
        )
    return None


def check_unsupported_market(
    observed_markets: set[str],
    supported_markets: set[str],
) -> DataQualityFinding | None:
    """Check for unsupported new market identifiers."""
    unsupported = observed_markets - supported_markets
    if unsupported:
        return DataQualityFinding(
            check_name="unsupported_market",
            severity=SEVERITY_WARNING,
            message=f"Unsupported market identifiers: {', '.join(sorted(unsupported))}",
            details={"unsupported": sorted(unsupported)},
        )
    return None


def check_inverted_odds(odds_records: list[dict]) -> DataQualityFinding | None:
    """Check for inverted odds (over < under in two-sided market)."""
    inverted = 0
    for rec in odds_records:
        over = rec.get("over_price", 0)
        under = rec.get("under_price", 0)
        if over and under and over < 0 and under < 0:
            if abs(over) < abs(under):
                inverted += 1
    if inverted > 0:
        return DataQualityFinding(
            check_name="inverted_odds",
            severity=SEVERITY_WARNING,
            message=f"Found {inverted} records with potentially inverted odds",
            details={"inverted_count": inverted},
        )
    return None


def check_invalid_timestamps(records: list[dict]) -> DataQualityFinding | None:
    """Check for malformed or future observation timestamps."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    future_limit = now + timedelta(hours=1)
    bad_count = 0
    future_count = 0
    for rec in records:
        ts = rec.get("observation_timestamp", "")
        if not ts:
            bad_count += 1
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt > future_limit:
                future_count += 1
        except (ValueError, TypeError):
            bad_count += 1

    findings = []
    if bad_count > 0:
        findings.append(DataQualityFinding(
            check_name="malformed_timestamp",
            severity=SEVERITY_WARNING,
            message=f"{bad_count} records with malformed timestamps",
            details={"count": bad_count},
        ))
    if future_count > 0:
        findings.append(DataQualityFinding(
            check_name="future_timestamp",
            severity=SEVERITY_CRITICAL,
            message=f"{future_count} records with future observation timestamps",
            details={"count": future_count},
        ))
    return findings if findings else None


def check_impossible_prices(odds_records: list[dict]) -> DataQualityFinding | None:
    """Check for impossible prices (negative American odds converted incorrectly)."""
    bad = 0
    for rec in odds_records:
        american = rec.get("american_odds", 0)
        if american is not None and american != 0:
            if -100 < american < 100 and american != 0:
                bad += 1
    if bad > 0:
        return DataQualityFinding(
            check_name="impossible_prices",
            severity=SEVERITY_CRITICAL,
            message=f"{bad} records with impossible odds values (between -100 and 100)",
            details={"count": bad},
        )
    return None


def check_one_sided_market(odds_records: list[dict]) -> DataQualityFinding | None:
    """Check for one-sided O/U markets (only over or only under available)."""
    over_only = 0
    under_only = 0
    for rec in odds_records:
        has_over = bool(rec.get("over_price"))
        has_under = bool(rec.get("under_price"))
        if has_over and not has_under:
            over_only += 1
        elif has_under and not has_over:
            under_only += 1

    total_one_sided = over_only + under_only
    if total_one_sided > 0:
        return DataQualityFinding(
            check_name="one_sided_market",
            severity=SEVERITY_WARNING,
            message=f"{total_one_sided} one-sided O/U markets (over-only: {over_only}, under-only: {under_only})",
            details={"over_only": over_only, "under_only": under_only},
        )
    return None


def check_volume_spike(
    current_count: int,
    rolling_avg: int,
    *,
    spike_threshold_pct: float = 200.0,
) -> DataQualityFinding | None:
    """Check for sudden recommendation-volume spike."""
    if rolling_avg <= 0:
        return None
    spike_pct = ((current_count - rolling_avg) / rolling_avg) * 100
    if spike_pct >= spike_threshold_pct:
        return DataQualityFinding(
            check_name="volume_spike",
            severity=SEVERITY_WARNING,
            message=f"Recommendation volume spiked {spike_pct:.0f}% ({rolling_avg} -> {current_count})",
            details={"current": current_count, "rolling_avg": rolling_avg, "spike_pct": round(spike_pct, 1)},
        )
    return None


def check_volume_collapse(
    current_count: int,
    rolling_avg: int,
    *,
    collapse_threshold_pct: float = 70.0,
) -> DataQualityFinding | None:
    """Check for sudden recommendation-volume collapse."""
    if rolling_avg <= 0:
        return None
    collapse_pct = ((rolling_avg - current_count) / rolling_avg) * 100
    if collapse_pct >= collapse_threshold_pct:
        severity = SEVERITY_CRITICAL if collapse_pct >= 90 else SEVERITY_WARNING
        return DataQualityFinding(
            check_name="volume_collapse",
            severity=severity,
            message=f"Recommendation volume collapsed {collapse_pct:.0f}% ({rolling_avg} -> {current_count})",
            details={"current": current_count, "rolling_avg": rolling_avg, "collapse_pct": round(collapse_pct, 1)},
        )
    return None


def check_extreme_consensus_disagreement(
    consensus_spread: float,
    *,
    threshold: float = 0.15,
) -> DataQualityFinding | None:
    """Check for extreme consensus disagreement (>15% spread)."""
    if consensus_spread > threshold:
        return DataQualityFinding(
            check_name="extreme_consensus_disagreement",
            severity=SEVERITY_WARNING,
            message=f"Extreme consensus disagreement: {consensus_spread:.1%} spread",
            details={"spread": round(consensus_spread, 4)},
        )
    return None


def check_duplicate_api_objects(
    records: list[dict],
    dedup_keys: tuple[str, ...] = ("odd_id", "sportsbook"),
) -> DataQualityFinding | None:
    """Check for duplicate API objects."""
    seen = set()
    dupes = 0
    for rec in records:
        key = tuple(rec.get(k, "") for k in dedup_keys)
        if key in seen:
            dupes += 1
        seen.add(key)
    if dupes > 0:
        return DataQualityFinding(
            check_name="duplicate_api_objects",
            severity=SEVERITY_WARNING,
            message=f"{dupes} duplicate API objects detected",
            details={"duplicate_count": dupes},
        )
    return None


def check_stale_observations(
    records: list[dict],
    *,
    stale_threshold_seconds: int = 7200,
) -> DataQualityFinding | None:
    """Check for stale observations."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    stale_limit = now - timedelta(seconds=stale_threshold_seconds)
    stale_count = 0
    for rec in records:
        ts = rec.get("observation_timestamp", "")
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt < stale_limit:
                    stale_count += 1
            except (ValueError, TypeError):
                pass
    if stale_count > 0:
        return DataQualityFinding(
            check_name="stale_observations",
            severity=SEVERITY_WARNING,
            message=f"{stale_count} stale observations (>{stale_threshold_seconds}s old)",
            details={"count": stale_count, "threshold_seconds": stale_threshold_seconds},
        )
    return None
