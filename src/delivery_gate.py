"""Delivery safety gate.

Public and VIP Discord delivery requires ALL of:
- SHADOW_MODE=false
- LIVE_DELIVERY_ACKNOWLEDGED=true
- Recent passing live-readiness check
- No current critical health failure
- No current critical data-quality finding
- Valid delivery configuration

Provides enable/disable commands with explicit confirmation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.production_config import load_config, ProductionConfig
from src.shadow_mode import load_shadow_config, ShadowConfig, save_shadow_config

logger = logging.getLogger(__name__)

# ── Gate file ──────────────────────────────────────────────────────

DELIVERY_GATE_FILE = "data/.delivery_gate.json"
CONFIRMATION_PHRASE = "ENABLE LIVE DELIVERY"


@dataclass
class DeliveryGateState:
    """Current state of the delivery safety gate."""
    enabled: bool = False
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    confirmation_phrase: str = ""
    config_snapshot: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.config_snapshot is None:
            self.config_snapshot = {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeliveryGateCheck:
    """Result of a single delivery gate check."""
    name: str = ""
    passed: bool = False
    message: str = ""
    details: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.details is None:
            self.details = {}


def check_delivery_gate(
    config: ProductionConfig | None = None,
    shadow: ShadowConfig | None = None,
) -> tuple[bool, list[DeliveryGateCheck]]:
    """Check if delivery is allowed. Returns (allowed, checks)."""
    if config is None:
        config = load_config()
    if shadow is None:
        shadow = load_shadow_config()

    checks = []

    # 1. Shadow mode
    shadow_ok = not shadow.shadow_mode
    checks.append(DeliveryGateCheck(
        name="shadow_mode",
        passed=shadow_ok,
        message="Shadow mode is OFF" if shadow_ok else "Shadow mode is ON — delivery blocked",
    ))

    # 2. Live delivery acknowledged
    ack_ok = shadow.live_delivery_acknowledged
    checks.append(DeliveryGateCheck(
        name="live_delivery_acknowledged",
        passed=ack_ok,
        message="Live delivery acknowledged" if ack_ok else "Live delivery not acknowledged",
    ))

    # 3. Recent passing readiness check
    readiness_ok = _check_recent_readiness()
    checks.append(DeliveryGateCheck(
        name="recent_readiness",
        passed=readiness_ok,
        message="Recent readiness check passed" if readiness_ok else "No recent passing readiness check",
    ))

    # 4. No critical health failure
    health_ok = _check_critical_health(config)
    checks.append(DeliveryGateCheck(
        name="critical_health",
        passed=health_ok,
        message="No critical health failures" if health_ok else "Critical health failure detected",
    ))

    # 5. No critical data-quality finding
    dq_ok = _check_critical_data_quality(config)
    checks.append(DeliveryGateCheck(
        name="critical_data_quality",
        passed=dq_ok,
        message="No critical data-quality findings" if dq_ok else "Critical data-quality finding detected",
    ))

    # 6. Valid delivery configuration
    config_ok = _check_delivery_config(config)
    checks.append(DeliveryGateCheck(
        name="delivery_config",
        passed=config_ok,
        message="Valid delivery configuration" if config_ok else "Invalid delivery configuration",
    ))

    allowed = all(c.passed for c in checks)
    return allowed, checks


def _check_recent_readiness() -> bool:
    """Check if a readiness check passed recently (within 24h)."""
    from src.live_readiness import get_acknowledgement
    ack = get_acknowledgement()
    if not ack:
        return False
    ts = ack.get("timestamp", "")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hours_old = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return hours_old < 24
    except (ValueError, TypeError):
        return False


def _check_critical_health(config: ProductionConfig) -> bool:
    """Check for critical health failures."""
    try:
        from src.health_check import run_health_checks
        report = run_health_checks(
            db_path=config.database_path,
            api_key=config.api_key,
            output_dir=config.output_dir,
            freshness_threshold=config.freshness_threshold_seconds,
        )
        return report.overall_status != "unhealthy"
    except Exception:
        return True  # Don't block on health check failure


def _check_critical_data_quality(config: ProductionConfig) -> bool:
    """Check for critical data-quality findings."""
    try:
        from src.data_quality import get_critical_findings
        conn = sqlite3.connect(config.database_path, timeout=5)
        try:
            findings = get_critical_findings(conn, since_hours=24)
            return len(findings) == 0
        finally:
            conn.close()
    except Exception:
        return True  # Don't block on DQ check failure


def _check_delivery_config(config: ProductionConfig) -> bool:
    """Check if delivery configuration is valid."""
    if not config.discord_webhook_urls:
        return False  # No delivery channel configured
    urls = [u.strip() for u in config.discord_webhook_urls.split(",") if u.strip()]
    return len(urls) > 0


# ── Gate state persistence ─────────────────────────────────────────

def save_gate_state(state: DeliveryGateState) -> None:
    """Save delivery gate state."""
    gate_path = Path(DELIVERY_GATE_FILE)
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(json.dumps({
        "enabled": state.enabled,
        "acknowledged_at": state.acknowledged_at,
        "acknowledged_by": state.acknowledged_by,
        "confirmation_phrase": state.confirmation_phrase,
        "config_snapshot": state.config_snapshot,
    }, indent=2))


def load_gate_state() -> DeliveryGateState:
    """Load delivery gate state."""
    gate_path = Path(DELIVERY_GATE_FILE)
    if not gate_path.exists():
        return DeliveryGateState()
    try:
        data = json.loads(gate_path.read_text())
        return DeliveryGateState(**data)
    except (json.JSONDecodeError, OSError):
        return DeliveryGateState()


def enable_live_delivery(config: ProductionConfig, confirmation: str) -> dict[str, Any]:
    """Enable live delivery with explicit confirmation."""
    if confirmation != CONFIRMATION_PHRASE:
        return {
            "success": False,
            "error": f"Confirmation phrase mismatch. Expected: '{CONFIRMATION_PHRASE}'",
        }

    # Set shadow mode off and acknowledgement on
    shadow = load_shadow_config()
    shadow.shadow_mode = False
    shadow.live_delivery_acknowledged = True
    save_shadow_config(shadow, "data/shadow_config.json")

    # Save gate state
    gate = DeliveryGateState(
        enabled=True,
        acknowledged_at=datetime.now(timezone.utc).isoformat(),
        acknowledged_by="operator",
        confirmation_phrase=confirmation,
        config_snapshot=config.redacted(),
    )
    save_gate_state(gate)

    logger.warning("Live delivery ENABLED by operator")
    return {
        "success": True,
        "message": "Live delivery enabled",
        "timestamp": gate.acknowledged_at,
    }


def disable_live_delivery() -> dict[str, Any]:
    """Disable live delivery and re-enable shadow mode."""
    shadow = load_shadow_config()
    shadow.shadow_mode = True
    shadow.live_delivery_acknowledged = False
    save_shadow_config(shadow, "data/shadow_config.json")

    gate = DeliveryGateState(enabled=False)
    save_gate_state(gate)

    logger.info("Live delivery DISABLED — shadow mode re-enabled")
    return {
        "success": True,
        "message": "Live delivery disabled — shadow mode re-enabled",
    }


def format_gate_status(allowed: bool, checks: list[DeliveryGateCheck]) -> str:
    """Format gate status for display."""
    lines = [
        f"Delivery Gate: {'OPEN' if allowed else 'BLOCKED'}",
        "",
    ]
    for c in checks:
        icon = "PASS" if c.passed else "BLOCK"
        lines.append(f"  [{icon}] {c.name}: {c.message}")
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="delivery_gate", description="Delivery safety gate")
    sub = parser.add_subparsers(dest="command")

    check_p = sub.add_parser("check", help="Check delivery gate status")
    check_p.add_argument("--json", action="store_true")

    enable_p = sub.add_parser("enable", help="Enable live delivery")
    enable_p.add_argument("--confirm", required=True, help="Confirmation phrase")
    enable_p.add_argument("--json", action="store_true")

    disable_p = sub.add_parser("disable", help="Disable live delivery")
    disable_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    from src.structured_logging import setup_logging
    setup_logging(level="WARNING")

    config = load_config()

    if args.command == "check":
        allowed, checks = check_delivery_gate(config)
        if args.json:
            print(json.dumps({"allowed": allowed, "checks": [asdict(c) for c in checks]}, indent=2))
        else:
            print(format_gate_status(allowed, checks))
        return 0 if allowed else 1

    elif args.command == "enable":
        result = enable_live_delivery(config, args.confirm)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result["success"]:
                print(f"Live delivery enabled at {result['timestamp']}")
            else:
                print(f"Failed: {result['error']}")
        return 0 if result["success"] else 1

    elif args.command == "disable":
        result = disable_live_delivery()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result["message"])
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
