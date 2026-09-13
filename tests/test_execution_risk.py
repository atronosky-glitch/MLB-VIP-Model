"""Tests for src/execution/risk.py. Pure -- no DB, fully-formed
RiskContext inputs, matching evaluator.py's own testing convention."""

from decimal import Decimal

from src.execution.paper.models import PaperRejectionReason
from src.execution.risk import RiskContext, RiskEngine


class _FakeConfig:
    max_bet_usd = 25.0
    max_bet_pct_bankroll = 0.025
    max_units_per_bet = 2.0
    max_event_exposure_usd = 50.0
    max_provider_exposure_usd = 250.0
    max_sport_exposure_usd = 300.0
    max_open_exposure_usd = 500.0
    max_daily_wagered_usd = 250.0
    max_daily_loss_usd = 100.0
    max_open_positions = 20
    max_trades_per_hour = 20
    min_paper_trade_usd = 1.0
    allow_risk_size_reduction = True
    allow_position_addons = False
    stop_after_daily_profit_target = False
    daily_profit_target_usd = 0.0


def _context(**overrides) -> RiskContext:
    defaults = dict(
        available_bankroll_usd=Decimal("1000"),
        open_positions_count=0,
        event_exposure_usd=Decimal("0"),
        provider_exposure_usd=Decimal("0"),
        sport_exposure_usd=Decimal("0"),
        total_open_exposure_usd=Decimal("0"),
        daily_wagered_usd=Decimal("0"),
        daily_realized_pnl_usd=Decimal("0"),
        trades_in_last_hour=0,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def _engine(**config_overrides) -> RiskEngine:
    config = _FakeConfig()
    for k, v in config_overrides.items():
        setattr(config, k, v)
    return RiskEngine(config)


class TestApprovalHappyPath:
    def test_recommended_stake_fully_approved_when_under_every_limit(self):
        engine = _engine()
        decision = engine.evaluate(Decimal("10"), Decimal("1"), Decimal("10"), _context(), False)
        assert decision.approved is True
        assert decision.approved_stake_usd == Decimal("10")
        assert decision.limiting_constraint is None


class TestMaxBetUsd:
    def test_caps_at_max_bet_usd(self):
        engine = _engine(max_bet_usd=20.0)
        decision = engine.evaluate(Decimal("30"), Decimal("3"), Decimal("10"), _context(), False)
        assert decision.approved is True
        assert decision.approved_stake_usd == Decimal("20")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_BET_USD

    def test_rejects_outright_when_size_reduction_disabled(self):
        engine = _engine(max_bet_usd=20.0, allow_risk_size_reduction=False)
        decision = engine.evaluate(Decimal("30"), Decimal("3"), Decimal("10"), _context(), False)
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.MAX_BET_USD


class TestMaxBetPctBankroll:
    def test_caps_at_pct_of_available_bankroll(self):
        engine = _engine(max_bet_pct_bankroll=0.01, max_bet_usd=1000)
        decision = engine.evaluate(
            Decimal("50"), Decimal("5"), Decimal("10"), _context(available_bankroll_usd=Decimal("1000")), False,
        )
        assert decision.approved_stake_usd == Decimal("10")  # 1% of 1000
        assert decision.limiting_constraint == PaperRejectionReason.MAX_BET_PCT_BANKROLL


class TestMaxUnitsPerBet:
    def test_caps_at_max_units_per_bet_converted_to_usd(self):
        engine = _engine(max_units_per_bet=1.0, max_bet_usd=1000)
        decision = engine.evaluate(Decimal("30"), Decimal("3"), Decimal("10"), _context(), False)
        assert decision.approved_stake_usd == Decimal("10")  # 1 unit * $10
        assert decision.limiting_constraint == PaperRejectionReason.MAX_UNITS_PER_BET


class TestMaxEventExposure:
    def test_caps_at_remaining_event_exposure_room(self):
        # example from the Stage 3 spec: existing $15 event exposure,
        # MAX_EVENT_EXPOSURE=$20, new proposed $10 -> only $5 room
        engine = _engine(max_event_exposure_usd=20.0, max_bet_usd=1000)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(event_exposure_usd=Decimal("15")), False,
        )
        assert decision.approved_stake_usd == Decimal("5")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_EVENT_EXPOSURE

    def test_full_worked_example_from_spec(self):
        """Sizing recommends $25; MAX_BET_USD=$20; bankroll pct cap=$15;
        event exposure remaining room=$8 -> approved stake=$8."""
        engine = _engine(max_bet_usd=20.0, max_bet_pct_bankroll=0.015, max_event_exposure_usd=58.0)
        decision = engine.evaluate(
            Decimal("25"), Decimal("2.5"), Decimal("10"),
            _context(available_bankroll_usd=Decimal("1000"), event_exposure_usd=Decimal("50")),
            False,
        )
        assert decision.approved_stake_usd == Decimal("8")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_EVENT_EXPOSURE


class TestMaxProviderExposure:
    def test_caps_at_remaining_provider_exposure_room(self):
        engine = _engine(max_provider_exposure_usd=100.0, max_bet_usd=1000)
        decision = engine.evaluate(
            Decimal("30"), Decimal("3"), Decimal("10"),
            _context(provider_exposure_usd=Decimal("90")), False,
        )
        assert decision.approved_stake_usd == Decimal("10")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_PROVIDER_EXPOSURE


class TestMaxSportExposure:
    def test_caps_at_remaining_sport_exposure_room(self):
        engine = _engine(max_sport_exposure_usd=100.0, max_bet_usd=1000)
        decision = engine.evaluate(
            Decimal("30"), Decimal("3"), Decimal("10"),
            _context(sport_exposure_usd=Decimal("95")), False,
        )
        assert decision.approved_stake_usd == Decimal("5")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_SPORT_EXPOSURE


class TestMaxOpenExposure:
    def test_caps_at_remaining_open_exposure_room(self):
        engine = _engine(max_open_exposure_usd=100.0, max_bet_usd=1000, max_units_per_bet=1000.0)
        decision = engine.evaluate(
            Decimal("30"), Decimal("3"), Decimal("10"),
            _context(total_open_exposure_usd=Decimal("80")), False,
        )
        assert decision.approved_stake_usd == Decimal("20")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_OPEN_EXPOSURE


class TestMaxDailyWagered:
    def test_caps_at_remaining_daily_wagered_room(self):
        engine = _engine(max_daily_wagered_usd=100.0, max_bet_usd=1000)
        decision = engine.evaluate(
            Decimal("30"), Decimal("3"), Decimal("10"),
            _context(daily_wagered_usd=Decimal("95")), False,
        )
        assert decision.approved_stake_usd == Decimal("5")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_DAILY_WAGERED


class TestDailyStopLoss:
    def test_exact_threshold_blocks(self):
        engine = _engine(max_daily_loss_usd=100.0)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(daily_realized_pnl_usd=Decimal("-100")), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.DAILY_STOP_LOSS_REACHED

    def test_one_cent_short_of_threshold_still_allows(self):
        engine = _engine(max_daily_loss_usd=100.0)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(daily_realized_pnl_usd=Decimal("-99.99")), False,
        )
        assert decision.approved is True

    def test_one_cent_beyond_threshold_blocks(self):
        engine = _engine(max_daily_loss_usd=100.0)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(daily_realized_pnl_usd=Decimal("-100.01")), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.DAILY_STOP_LOSS_REACHED

    def test_does_not_trigger_on_a_profitable_day(self):
        engine = _engine(max_daily_loss_usd=100.0)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(daily_realized_pnl_usd=Decimal("50")), False,
        )
        assert decision.approved is True


class TestDailyProfitTarget:
    def test_disabled_by_default_never_blocks(self):
        engine = _engine(stop_after_daily_profit_target=False, daily_profit_target_usd=50.0)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(daily_realized_pnl_usd=Decimal("100")), False,
        )
        assert decision.approved is True

    def test_enabled_and_reached_blocks(self):
        engine = _engine(stop_after_daily_profit_target=True, daily_profit_target_usd=50.0)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(daily_realized_pnl_usd=Decimal("50")), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.DAILY_PROFIT_TARGET_REACHED

    def test_enabled_but_not_yet_reached_allows(self):
        engine = _engine(stop_after_daily_profit_target=True, daily_profit_target_usd=50.0)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"),
            _context(daily_realized_pnl_usd=Decimal("49.99")), False,
        )
        assert decision.approved is True


class TestMaxOpenPositions:
    def test_blocks_at_the_limit(self):
        engine = _engine(max_open_positions=5)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(open_positions_count=5), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.MAX_OPEN_POSITIONS

    def test_allows_just_under_the_limit(self):
        engine = _engine(max_open_positions=5)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(open_positions_count=4), False,
        )
        assert decision.approved is True


class TestMaxTradesPerHour:
    def test_blocks_at_the_limit(self):
        engine = _engine(max_trades_per_hour=3)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(trades_in_last_hour=3), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.MAX_TRADES_PER_HOUR


class TestBankrollAndMinTrade:
    def test_zero_bankroll_rejects_as_bankroll_too_low(self):
        engine = _engine()
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(available_bankroll_usd=Decimal("0")), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.BANKROLL_TOO_LOW

    def test_approved_stake_below_min_trade_rejects_as_bet_too_small(self):
        engine = _engine(min_paper_trade_usd=5.0, max_event_exposure_usd=2.0, max_bet_usd=1000)
        decision = engine.evaluate(Decimal("10"), Decimal("1"), Decimal("10"), _context(), False)
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.BET_TOO_SMALL


class TestDuplicateDetection:
    def test_open_duplicate_blocks_when_addons_disabled(self):
        engine = _engine(allow_position_addons=False)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(has_open_duplicate=True), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.DUPLICATE_POSITION

    def test_open_duplicate_allowed_when_addons_enabled(self):
        engine = _engine(allow_position_addons=True)
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(has_open_duplicate=True), False,
        )
        assert decision.approved is True

    def test_settled_duplicate_blocks_by_default(self):
        engine = _engine()
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(has_settled_duplicate=True), False,
        )
        assert decision.approved is False
        assert decision.rejection_reason == PaperRejectionReason.ALREADY_TRADED_RECOMMENDATION

    def test_settled_duplicate_allowed_when_explicitly_configured(self):
        engine = _engine()
        decision = engine.evaluate(
            Decimal("10"), Decimal("1"), Decimal("10"), _context(has_settled_duplicate=True), True,
        )
        assert decision.approved is True


class TestMostRestrictiveWins:
    def test_multiple_binding_constraints_picks_the_tightest(self):
        engine = _engine(max_bet_usd=20.0, max_event_exposure_usd=1000.0, max_provider_exposure_usd=1000.0)
        decision = engine.evaluate(
            Decimal("30"), Decimal("3"), Decimal("10"),
            _context(event_exposure_usd=Decimal("990")),  # event room = 10, tighter than max_bet_usd=20
            False,
        )
        # event exposure (1000-990=10) is tighter than max_bet_usd=20
        assert decision.approved_stake_usd == Decimal("10")
        assert decision.limiting_constraint == PaperRejectionReason.MAX_EVENT_EXPOSURE
