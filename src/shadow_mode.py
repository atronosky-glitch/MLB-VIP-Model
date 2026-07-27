"""Shadow mode configuration and enforcement.

SHADOW_MODE=true (default) blocks public/VIP recommendation delivery
while allowing all internal analysis, storage, grading, and reporting.

Shadow mode is the default for production deployment. It must be
explicitly disabled by an operator who has completed the promotion
criteria review.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# ── Shadow mode defaults ───────────────────────────────────────────

SHADOW_MODE_DEFAULT = True
LIVE_DELIVERY_ACKNOWLEDGED_DEFAULT = False

# ── Environment variable mapping ───────────────────────────────────

SHADOW_ENV_MAP = {
    "MLB_SHADOW_MODE": "shadow_mode",
    "MLB_LIVE_DELIVERY_ACKNOWLEDGED": "live_delivery_acknowledged",
}


@dataclass
class ShadowConfig:
    """Shadow mode and delivery configuration."""
    shadow_mode: bool = True
    live_delivery_acknowledged: bool = False

    def is_delivery_blocked(self) -> bool:
        """Public/VIP delivery is blocked when shadow mode is on."""
        return self.shadow_mode

    def can_enable_delivery(self) -> bool:
        """Check if delivery can be enabled (all gates open)."""
        return not self.shadow_mode and self.live_delivery_acknowledged

    def block_reasons(self) -> list[str]:
        """Return reasons why delivery is blocked."""
        reasons = []
        if self.shadow_mode:
            reasons.append("SHADOW_MODE=true")
        if not self.live_delivery_acknowledged:
            reasons.append("LIVE_DELIVERY_ACKNOWLEDGED not set")
        return reasons


def load_shadow_config(config_path: str | None = None) -> ShadowConfig:
    """Load shadow mode configuration from environment and optional config file.

    Priority: environment variables > config file > defaults.
    """
    shadow_mode = SHADOW_MODE_DEFAULT
    live_delivery = LIVE_DELIVERY_ACKNOWLEDGED_DEFAULT

    # Load from config file if provided
    if config_path:
        import json
        from pathlib import Path
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            if "shadow_mode" in data:
                shadow_mode = bool(data["shadow_mode"])
            if "live_delivery_acknowledged" in data:
                live_delivery = bool(data["live_delivery_acknowledged"])

    # Environment overrides
    env_shadow = os.environ.get("MLB_SHADOW_MODE")
    if env_shadow is not None:
        shadow_mode = env_shadow.lower() in ("true", "1", "yes")

    env_delivery = os.environ.get("MLB_LIVE_DELIVERY_ACKNOWLEDGED")
    if env_delivery is not None:
        live_delivery = env_delivery.lower() in ("true", "1", "yes")

    return ShadowConfig(
        shadow_mode=shadow_mode,
        live_delivery_acknowledged=live_delivery,
    )


def save_shadow_config(config: ShadowConfig, path: str | Path) -> None:
    """Save shadow mode configuration to a JSON file."""
    import json
    from pathlib import Path
    Path(path).write_text(json.dumps({
        "shadow_mode": config.shadow_mode,
        "live_delivery_acknowledged": config.live_delivery_acknowledged,
    }, indent=2))
