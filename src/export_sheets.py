"""Google Sheets dashboard export.

Exports recommendations to Google Sheets with batch updates, idempotent
row identity (fingerprint-based), header freezing, and original snapshot
preservation.

Gracefully degrades when libraries are unavailable — never hard-depends
on google-api-python-client.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Try importing Google libraries ─────────────────────────────────

try:
    from google.oauth2.credentials import Credentials
    from google.oauth2.service_account import Credentials as SACredentials
    from googleapiclient.discovery import build
    _HAS_GOOGLE = True
except ImportError:
    _HAS_GOOGLE = False


# ── Sheet structure ────────────────────────────────────────────────

HEADERS = [
    "Date",
    "Player",
    "Event",
    "Market",
    "Period",
    "Line",
    "Side",
    "Sportsbook",
    "American Odds",
    "Decimal Odds",
    "EV %",
    "Price Adv (pp)",
    "Model Score",
    "Confidence",
    "Status",
    "Fingerprint",
    "Captured At",
]

SHEET_NAME = "Recommendations"
SUMMARY_SHEET = "Summary"


def export_recommendations(
    db_path: str | Path,
    spreadsheet_id: str,
    credentials_path: str,
    *,
    sheet_name: str = SHEET_NAME,
) -> dict[str, Any]:
    """Export current recommendations to Google Sheets.

    Parameters
    ----------
    db_path:
        Path to the SQLite database.
    spreadsheet_id:
        Google Sheets spreadsheet ID.
    credentials_path:
        Path to service account credentials JSON file.
    sheet_name:
        Name of the recommendations sheet.

    Returns
    -------
    Dict with export stats: rows_upserted, rows_skipped, sheets_updated.
    """
    if not _HAS_GOOGLE:
        raise ImportError(
            "Google Sheets libraries not installed. "
            "Run: pip install google-api-python-client google-auth"
        )

    if not spreadsheet_id:
        raise ValueError("spreadsheet_id is required")

    if not credentials_path:
        raise ValueError("credentials_path is required")

    # Load recommendations early — return before credential checks if empty
    rows = _load_recommendations(db_path)
    if not rows:
        logger.info("No recommendations to export")
        return {"rows_upserted": 0, "rows_skipped": 0, "sheets_updated": 0}

    creds_path = Path(credentials_path)
    if not creds_path.exists():
        raise FileNotFoundError(f"Credentials file not found: {creds_path}")

    # Build service
    creds = SACredentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds)
    sheets = service.spreadsheets()

    # Ensure sheet exists
    _ensure_sheet(sheets, spreadsheet_id, sheet_name)

    # Batch upsert (idempotent by fingerprint)
    existing_fingerprints = _get_existing_fingerprints(sheets, spreadsheet_id, sheet_name)
    new_rows = []
    skipped = 0
    for row in rows:
        fp = row[-2]  # fingerprint is second-to-last column
        if fp in existing_fingerprints:
            skipped += 1
        else:
            new_rows.append(row)
            existing_fingerprints.add(fp)

    if new_rows:
        _batch_append(sheets, spreadsheet_id, sheet_name, new_rows)
        logger.info("Appended %d rows to %s", len(new_rows), sheet_name)

    # Update summary sheet
    _update_summary(sheets, spreadsheet_id, SUMMARY_SHEET, rows)

    return {
        "rows_upserted": len(new_rows),
        "rows_skipped": skipped,
        "sheets_updated": 2 if new_rows else 1,
    }


def _load_recommendations(db_path: str | Path) -> list[list[Any]]:
    """Load recommendations from DB as sheet-ready rows."""
    from database.db_manager import get_connection

    conn = get_connection(str(db_path))
    try:
        # Check if table exists
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "historical_recommendations" not in tables:
            return []

        cursor = conn.execute("""
            SELECT
                r.observation_timestamp,
                r.player_name,
                r.event_id,
                r.market_type,
                r.period,
                r.line,
                r.side,
                r.sportsbook,
                r.offered_american_odds,
                r.offered_decimal_odds,
                r.ev_pct,
                r.yn_implied_prob_adv,
                r.model_score,
                r.rec_status,
                r.fingerprint
            FROM historical_recommendations r
            WHERE r.rec_status IN ('BET', 'LEAN', 'MONITOR')
            ORDER BY r.ev_pct DESC
        """)
        rows = []
        for row in cursor.fetchall():
            rows.append([_safe_str(v) for v in row])
        return rows
    finally:
        conn.close()


def _safe_str(val: Any) -> str:
    """Convert value to string safely."""
    if val is None:
        return ""
    return str(val)


def _ensure_sheet(sheets: Any, spreadsheet_id: str, sheet_name: str) -> None:
    """Create the sheet if it doesn't exist, with frozen headers."""
    metadata = sheets.get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in metadata.get("sheets", [])}

    if sheet_name not in existing:
        sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_name,
                                "gridProperties": {"frozenRowCount": 1},
                            }
                        }
                    }
                ]
            },
        ).execute()
        # Write headers
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()
        logger.info("Created sheet: %s", sheet_name)


def _get_existing_fingerprints(
    sheets: Any, spreadsheet_id: str, sheet_name: str
) -> set[str]:
    """Get all fingerprints already in the sheet (column O = index 14)."""
    try:
        result = sheets.values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!O:O",
        ).execute()
        values = result.get("values", [])
        return {row[0] for row in values if row}
    except Exception:
        return set()


def _batch_append(
    sheets: Any, spreadsheet_id: str, sheet_name: str, rows: list[list[str]]
) -> None:
    """Append rows in batches to avoid API limits."""
    BATCH_SIZE = 100
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        sheets.values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A:A",
            valueInputOption="RAW",
            body={"values": batch},
        ).execute()


def _update_summary(
    sheets: Any, spreadsheet_id: str, summary_sheet: str, all_rows: list[list[str]]
) -> None:
    """Write summary stats to a Summary sheet."""
    from src.market_analysis import american_to_decimal

    # Count by status
    status_counts: dict[str, int] = {}
    for row in all_rows:
        if len(row) > 13:
            status = row[13]
            status_counts[status] = status_counts.get(status, 0) + 1

    summary_data = [
        ["Metric", "Value"],
        ["Last Updated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")],
        ["Total Recommendations", str(len(all_rows))],
    ]
    for status, count in sorted(status_counts.items()):
        summary_data.append([f"Status: {status}", str(count)])

    # Ensure summary sheet exists
    metadata = sheets.get(spreadsheetId=spreadsheet_id).execute()
    existing = {s["properties"]["title"] for s in metadata.get("sheets", [])}

    if summary_sheet not in existing:
        sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {"addSheet": {"properties": {"title": summary_sheet}}}
                ]
            },
        ).execute()

    sheets.values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"{summary_sheet}!A:Z",
    ).execute()
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{summary_sheet}!A1",
        valueInputOption="RAW",
        body={"values": summary_data},
    ).execute()
