"""Architecture tests enforcing Stage 4's central safety invariant
(spec section 9): no file outside src/execution/live/service.py and
the provider modules themselves may reference `_submit_authorized_order`,
and place_order/cancel_order must remain permanently NotImplemented on
every provider, unchanged from Stage 1.

These are static/grep-based checks, not behavioral tests -- they
protect against a future change accidentally introducing a second
call site for the real live-mutation entry point.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

_ALLOWED_REFERENCE_FILES = {
    SRC_DIR / "execution" / "live" / "service.py",
    SRC_DIR / "execution" / "live" / "__init__.py",  # docstring mention only
    SRC_DIR / "execution" / "base.py",
    SRC_DIR / "execution" / "kalshi.py",
    SRC_DIR / "execution" / "polymarket_us.py",
}


def _all_python_files() -> list[Path]:
    return [p for p in SRC_DIR.rglob("*.py")]


class TestSubmitAuthorizedOrderIsIsolated:
    def test_only_allowed_files_reference_submit_authorized_order(self):
        offenders = []
        for path in _all_python_files():
            if path in _ALLOWED_REFERENCE_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            if "_submit_authorized_order" in text:
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
        assert offenders == [], (
            f"_submit_authorized_order referenced outside the allowed files: {offenders}. "
            "Only src/execution/live/service.py may call this; base.py/kalshi.py/polymarket_us.py "
            "only define it."
        )

    def test_service_py_is_the_only_caller_not_just_a_definer(self):
        """Confirms service.py doesn't just mention the name in a
        comment -- it actually calls it."""
        service_text = (SRC_DIR / "execution" / "live" / "service.py").read_text(encoding="utf-8")
        assert re.search(r"\._submit_authorized_order\s*\(", service_text), (
            "service.py should actually call provider._submit_authorized_order(...)"
        )

    def test_no_cli_command_other_than_live_execute_references_submission(self):
        """paper_cli.py, cli.py's Stage 2B commands, and live_cli.py's
        own non-execute commands must never reference the submission
        entry point -- only live_cli.py's live_execute function may
        (via LiveExecutionService, not directly)."""
        for filename in ("cli.py", "paper_cli.py"):
            text = (SRC_DIR / "execution" / filename).read_text(encoding="utf-8")
            assert "_submit_authorized_order" not in text, f"{filename} must never reference _submit_authorized_order"

        live_cli_text = (SRC_DIR / "execution" / "live_cli.py").read_text(encoding="utf-8")
        assert "_submit_authorized_order" not in live_cli_text, (
            "live_cli.py must call LiveExecutionService.execute_authorized(), never the provider method directly"
        )


class TestPlaceOrderStillPermanentlyBlocked:
    """Regression: Stage 4 must not have loosened Stage 1's place_order/
    cancel_order guarantee in any way."""

    def test_kalshi_place_order_still_raises(self):
        from src.execution.kalshi import KalshiProvider
        provider = object.__new__(KalshiProvider)
        with pytest.raises(NotImplementedError):
            provider.place_order()

    def test_kalshi_cancel_order_still_raises(self):
        from src.execution.kalshi import KalshiProvider
        provider = object.__new__(KalshiProvider)
        with pytest.raises(NotImplementedError):
            provider.cancel_order()

    def test_polymarket_place_order_still_raises(self):
        from src.execution.polymarket_us import PolymarketUSProvider
        provider = object.__new__(PolymarketUSProvider)
        with pytest.raises(NotImplementedError):
            provider.place_order()

    def test_polymarket_cancel_order_still_raises(self):
        from src.execution.polymarket_us import PolymarketUSProvider
        provider = object.__new__(PolymarketUSProvider)
        with pytest.raises(NotImplementedError):
            provider.cancel_order()

    def test_base_class_place_order_source_is_unchanged_raise(self):
        import inspect
        from src.execution.base import PredictionMarketProvider
        source = inspect.getsource(PredictionMarketProvider.place_order)
        assert "raise NotImplementedError" in source

    def test_base_class_cancel_order_source_is_unchanged_raise(self):
        import inspect
        from src.execution.base import PredictionMarketProvider
        source = inspect.getsource(PredictionMarketProvider.cancel_order)
        assert "raise NotImplementedError" in source


class TestKalshiFailsClosedByConstruction:
    def test_kalshi_live_schema_verified_constant_is_false(self):
        from src.execution.kalshi import KALSHI_LIVE_SCHEMA_VERIFIED
        assert KALSHI_LIVE_SCHEMA_VERIFIED is False

    def test_kalshi_live_schema_verified_is_a_plain_module_constant(self):
        """Must be a hard-coded module constant, not read from
        config/env -- so no .env edit can ever flip it."""
        import src.execution.kalshi as kalshi_module
        text = Path(kalshi_module.__file__).read_text(encoding="utf-8")
        assert "KALSHI_LIVE_SCHEMA_VERIFIED = False" in text
        assert "config.kalshi_live_schema_verified" not in text.lower()
        assert "os.environ" not in text


class TestDefaultLiveConfigStaysOff:
    def test_all_live_flags_default_false_or_conservative(self):
        from src.production_config import ProductionConfig
        defaults = ProductionConfig()
        assert defaults.live_trading_enabled is False
        assert defaults.kalshi_live_enabled is False
        assert defaults.polymarket_us_live_enabled is False
        assert defaults.require_human_approval is True
        assert defaults.max_financial_post_attempts_per_approval == 1
