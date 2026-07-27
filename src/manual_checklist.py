"""Pre-live verification checklist.

Manual verification steps that must be completed before transitioning
from shadow mode to live delivery. These cannot be automated — they
require human judgment and sign-off.

Tracks completion status per item and provides CLI for management.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHECKLIST_FILE = "data/.pre_live_checklist.json"


@dataclass
class ChecklistItem:
    """A single manual verification item."""
    id: str = ""
    category: str = ""
    title: str = ""
    description: str = ""
    completed: bool = False
    completed_at: str = ""
    completed_by: str = ""
    notes: str = ""
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Default checklist ──────────────────────────────────────────────

DEFAULT_CHECKLIST: list[dict] = [
    # Delivery Safety
    {
        "id": "delivery-01",
        "category": "delivery_safety",
        "title": "Review Discord webhook URLs",
        "description": "Verify webhook URLs point to the correct channels (private test channels). Confirm no production channels are configured accidentally.",
    },
    {
        "id": "delivery-02",
        "category": "delivery_safety",
        "title": "Verify message formatting",
        "description": "Inspect sample Discord messages for correct formatting, no garbled text, proper chunking for long messages, and appropriate emoji/icons.",
    },
    {
        "id": "delivery-03",
        "category": "delivery_safety",
        "title": "Confirm delivery rate limiting",
        "description": "Verify rate limiter prevents Discord429 errors. Test with rapid sequential sends.",
    },
    {
        "id": "delivery-04",
        "category": "delivery_safety",
        "title": "Validate confidence filter thresholds",
        "description": "Review min_confidence_score and min_ev_pct settings. Confirm they produce actionable but not excessive recommendations.",
    },
    # Data Quality
    {
        "id": "data-01",
        "category": "data_quality",
        "title": "Inspect raw API response schemas",
        "description": "Verify EventOdds response structure matches parser expectations. Check for unexpected null fields or changed data types.",
    },
    {
        "id": "data-02",
        "category": "data_quality",
        "title": "Validate sportsbook mappings",
        "description": "Confirm all expected sportsbooks (DraftKings, FanDuel, BetMGM, Caesars, etc.) are correctly mapped and producing odds.",
    },
    {
        "id": "data-03",
        "category": "data_quality",
        "title": "Validate market mappings",
        "description": "Confirm all 20 market types are correctly parsed. Spot-check player names, lines, and odds against sportsbook websites.",
    },
    {
        "id": "data-04",
        "category": "data_quality",
        "title": "Review data-quality findings",
        "description": "Review all CRITICAL and WARNING findings from the last 7 days. Confirm none indicate systematic data corruption.",
    },
    # YN Markets
    {
        "id": "yn-01",
        "category": "yn_markets",
        "title": "Manual YN odds spot-check",
        "description": "Compare system YN odds against 3+ sportsbook websites for 5+ player props. Confirm prices match (within rounding tolerance).",
    },
    {
        "id": "yn-02",
        "category": "yn_markets",
        "title": "YN line validation",
        "description": "Verify player names are correctly extracted from API data. Confirm no name-swap issues between Yes/No sides.",
    },
    {
        "id": "yn-03",
        "category": "yn_markets",
        "title": "YN recommendation review",
        "description": "Manually review 10+ YN recommendations. Confirm no obvious errors (wrong player, wrong market, impossible odds).",
    },
    # System
    {
        "id": "sys-01",
        "category": "system",
        "title": "Verify backup and restore",
        "description": "Create a backup, then restore it to a test location. Confirm the restored database is intact and queryable.",
    },
    {
        "id": "sys-02",
        "category": "system",
        "title": "Verify scheduler configuration",
        "description": "Confirm scheduler generates correct crontab/Task Scheduler entries. Verify timezone handling.",
    },
    {
        "id": "sys-03",
        "category": "system",
        "title": "Verify health check accuracy",
        "description": "Run health checks and manually verify each check is reporting correctly. Induce a known failure and confirm detection.",
    },
    {
        "id": "sys-04",
        "category": "system",
        "title": "Verify logging output",
        "description": "Run a full pipeline and inspect logs. Confirm structured JSON logging works, no sensitive data in logs, proper log levels.",
    },
    # Monitoring
    {
        "id": "mon-01",
        "category": "monitoring",
        "title": "Review shadow dashboard",
        "description": "Review shadow-run dashboard. Confirm all sections are populated and accurate.",
    },
    {
        "id": "mon-02",
        "category": "monitoring",
        "title": "Review promotion criteria",
        "description": "Review all promotion criteria. Confirm each criterion is correctly evaluated.",
    },
]


def load_checklist() -> list[ChecklistItem]:
    """Load checklist from file or create defaults."""
    path = Path(CHECKLIST_FILE)
    if not path.exists():
        items = []
        for item_data in DEFAULT_CHECKLIST:
            items.append(ChecklistItem(**item_data))
        save_checklist(items)
        return items

    try:
        data = json.loads(path.read_text())
        return [ChecklistItem(**item) for item in data]
    except (json.JSONDecodeError, OSError):
        items = []
        for item_data in DEFAULT_CHECKLIST:
            items.append(ChecklistItem(**item_data))
        save_checklist(items)
        return items


def save_checklist(items: list[ChecklistItem]) -> None:
    """Save checklist to file."""
    path = Path(CHECKLIST_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([item.to_dict() for item in items], indent=2))


def complete_item(item_id: str, notes: str = "", completed_by: str = "operator") -> bool:
    """Mark a checklist item as completed."""
    items = load_checklist()
    for item in items:
        if item.id == item_id:
            item.completed = True
            item.completed_at = datetime.now(timezone.utc).isoformat()
            item.completed_by = completed_by
            item.notes = notes
            save_checklist(items)
            return True
    return False


def uncomplete_item(item_id: str) -> bool:
    """Mark a checklist item as not completed."""
    items = load_checklist()
    for item in items:
        if item.id == item_id:
            item.completed = False
            item.completed_at = ""
            item.completed_by = ""
            save_checklist(items)
            return True
    return False


def get_checklist_status() -> dict[str, Any]:
    """Get current checklist status."""
    items = load_checklist()
    required_items = [i for i in items if i.required]
    optional_items = [i for i in items if not i.required]

    required_completed = sum(1 for i in required_items if i.completed)
    optional_completed = sum(1 for i in optional_items if i.completed)

    all_required_met = required_completed == len(required_items)

    # Category breakdown
    categories = {}
    for item in items:
        cat = item.category
        if cat not in categories:
            categories[cat] = {"total": 0, "completed": 0}
        categories[cat]["total"] += 1
        if item.completed:
            categories[cat]["completed"] += 1

    return {
        "all_required_met": all_required_met,
        "required_total": len(required_items),
        "required_completed": required_completed,
        "optional_total": len(optional_items),
        "optional_completed": optional_completed,
        "categories": categories,
        "items": [item.to_dict() for item in items],
    }


def format_checklist(status: dict[str, Any] | None = None) -> str:
    """Format checklist for display."""
    if status is None:
        status = get_checklist_status()

    lines = [
        f"Pre-Live Verification Checklist",
        f"  Required: {status['required_completed']}/{status['required_total']}",
        f"  Optional: {status['optional_completed']}/{status['optional_total']}",
        "",
    ]

    # Group by category
    items = status["items"]
    categories = {}
    for item in items:
        cat = item["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(item)

    for cat, cat_items in categories.items():
        lines.append(f"--- {cat.upper().replace('_', ' ')} ---")
        for item in cat_items:
            icon = "DONE" if item["completed"] else ("NEED" if item["required"] else "opts")
            req = "*" if item["required"] else " "
            lines.append(f"  [{icon}{req}] {item['id']}: {item['title']}")
            if item["completed"] and item.get("completed_at"):
                lines.append(f"         Completed: {item['completed_at']}")
            if item["notes"]:
                lines.append(f"         Notes: {item['notes']}")
        lines.append("")

    if status["all_required_met"]:
        lines.append("ALL REQUIRED ITEMS COMPLETE. Ready to proceed.")
    else:
        remaining = status["required_total"] - status["required_completed"]
        lines.append(f"{remaining} required items remaining.")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="manual_checklist", description="Pre-live verification checklist")
    sub = parser.add_subparsers(dest="command")

    status_p = sub.add_parser("status", help="Show checklist status")
    status_p.add_argument("--json", action="store_true")

    complete_p = sub.add_parser("complete", help="Mark item complete")
    complete_p.add_argument("item_id", help="Item ID to complete")
    complete_p.add_argument("--notes", default="", help="Completion notes")
    complete_p.add_argument("--by", default="operator", help="Completed by")

    uncomplete_p = sub.add_parser("uncomplete", help="Mark item incomplete")
    uncomplete_p.add_argument("item_id", help="Item ID to uncomplete")

    args = parser.parse_args(argv)

    from src.structured_logging import setup_logging
    setup_logging(level="WARNING")

    if args.command == "status":
        status = get_checklist_status()
        if args.json:
            print(json.dumps(status, indent=2))
        else:
            print(format_checklist(status))
        return 0

    elif args.command == "complete":
        if complete_item(args.item_id, notes=args.notes, completed_by=args.by):
            print(f"Item {args.item_id} marked complete")
            return 0
        print(f"Item {args.item_id} not found")
        return 1

    elif args.command == "uncomplete":
        if uncomplete_item(args.item_id):
            print(f"Item {args.item_id} marked incomplete")
            return 0
        print(f"Item {args.item_id} not found")
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
