# Phase 9 Audit Report — Intelligence Layer

**Date**: 2026-07-23
**Status**: COMPLETE — 723/723 tests passing (682 original + 41 new)

---

## Summary

Phase 9 adds the intelligence layer: historical analytics, closing line tracking, confidence scoring, bookmaker quality rankings, calibration analysis, and CSV report generation. The system now answers "is +2% EV profitable?" from historical data rather than relying on fixed assumptions.

## Deliverables by Part

### Part A — Closing Line Database

| Component | Location | Status |
|---|---|---|
| `capture_closing_prices()` | `database/db_manager.py:1077` | Complete |
| `get_all_recommendations_with_settlement()` | `database/db_manager.py:1140` | Complete |
| Pipeline integration | `src/daily_pipeline.py` freeze stage | Complete |

**How it works**: When recommendations are frozen, `capture_closing_prices()` looks up the latest odds for each recommendation from `player_prop_odds`, computes CLV probability (`bet_implied_prob - closing_implied_prob`), and stores in `closing_prices`. Idempotent — skips already-captured records.

### Part B — Historical Analytics Engine

| Function | Purpose |
|---|---|
| `roi_by_market()` | ROI breakdown by market_type |
| `roi_by_sportsbook()` | ROI breakdown by sportsbook |
| `roi_by_rec_status()` | ROI by recommendation status (STRONG_EDGE, etc.) |
| `roi_by_ev_bucket()` | ROI by configurable EV bucket (converts decimal to percentage points) |
| `roi_by_odds_bucket()` | ROI by American odds range |
| `roi_by_n_books()` | ROI by comparison-book count |
| `roi_by_day()` | ROI by scan date |
| `roi_by_hour_before_pitch()` | ROI by hours before first pitch |
| `clv_by_sportsbook()` | CLV metrics per sportsbook |
| `clv_by_market()` | CLV metrics per market type |
| `hit_rate_by_market()` | Alias for roi_by_market |
| `overall_summary()` | Aggregate performance metrics |

All functions are pure SQL aggregation — no Python-side grouping.

### Part C — Recommendation Calibration

| Component | Location |
|---|---|
| `analyze_calibration()` | `src/calibration.py` |

Identifies profitable/unprofitable adjacent EV buckets and generates threshold-adjustment recommendations. Never auto-changes thresholds. Returns `recommendations` list with human-readable `reason` strings.

### Part D — Bookmaker Quality Scores

| Component | Location |
|---|---|
| `bookmaker_quality_scores()` | `src/bookmaker_scores.py` |
| `bookmaker_disagreement()` | `src/bookmaker_scores.py` |

Quality score = CLV score (0-50) + ROI score (0-50). Higher = better. Disagreement measures odds divergence from fair odds.

### Part E — Recommendation Confidence

| Component | Location |
|---|---|
| `compute_confidence()` | `src/confidence.py` |
| `ConfidenceWeights` | `src/confidence.py` |
| `CONFIDENCE_WEIGHTS` | `src/prop_config.py` |

Five components, each normalized to 0-1, weighted and scaled to 0-100:

| Component | Default Weight | What it measures |
|---|---|---|
| n_books | 2.0 | Comparison-book count (0 = 0 books, 1.0 = 8+ books) |
| market_quality | 1.5 | VALID_MARKET=1.0, NEEDS_REVIEW=0.5, INSUFFICIENT=0.2, EXCLUDED=0.0 |
| ev_magnitude | 2.5 | EV as % of max 15% (0% = 0.0, 15%+ = 1.0) |
| freshness | 1.0 | LIVE=1.0, CACHE=0.7, STALE=0.3 |
| mapping_confidence | 1.0 | HIGH=1.0, MEDIUM=0.7, LOW=0.3, NONE=0.0 |

Grades: A (80+), B (60+), C (40+), D (20+), F (<20).

### Part F — Reports

| Report | File | Contents |
|---|---|---|
| Performance | `performance_report.csv` | Overall summary (total, wins, losses, ROI, win_rate) |
| Sportsbook | `sportsbook_report.csv` | Quality rankings per book (score, CLV, ROI, settled count) |
| Market | `market_report.csv` | ROI and CLV by market type |
| Recommendation | `recommendation_report.csv` | All recs with confidence scores |
| Confidence | `confidence_report.csv` | Score distribution (A-F counts, avg score, component averages) |

### Part G — Tests

| Test Class | Tests | Coverage |
|---|---|---|
| TestCLVCapture | 6 | Closing price storage, CLV calculation, capture from odds, skip existing |
| TestAnalytics | 9 | All ROI breakdowns, CLV breakdowns, overall summary, hit rate |
| TestConfidenceScoring | 6 | High/low quality, YN advantage, grade boundaries, normalization, custom weights |
| TestCalibration | 2 | Bucket analysis, empty data |
| TestBookmakerScores | 2 | Quality rankings, empty data |
| TestReports | 7 | All 5 reports, batch generation, empty data |
| TestBuckets | 3 | EV, odds, N_books bucket assignment |
| TestDBHelpers | 1 | get_all_recommendations_with_settlement |
| TestComputeUnits | 5 | Win/loss/push/unresolved, positive/negative odds |
| **Total** | **41** | |

## Files Changed

| File | Change |
|---|---|
| `src/analytics.py` | **NEW** — 12 analytics query functions |
| `src/calibration.py` | **NEW** — calibration analyzer |
| `src/bookmaker_scores.py` | **NEW** — bookmaker quality scores |
| `src/confidence.py` | **NEW** — confidence scoring engine |
| `src/reports.py` | **NEW** — 5 CSV report generators |
| `database/db_manager.py` | Added `capture_closing_prices()`, `get_all_recommendations_with_settlement()` |
| `src/daily_pipeline.py` | Freeze stage captures closing prices; imports `capture_closing_prices` |
| `src/prop_config.py` | Added `CONFIDENCE_WEIGHTS` dict |
| `tests/test_phase9_intelligence.py` | **NEW** — 41 tests |

## Regression Check

- Full suite: **723/723 passing**
- No tests skipped
- No tests failed
- All existing Phase 1-8 tests pass unchanged

## Key Design Decisions

1. **Closing prices at freeze time** — automated, idempotent, captures latest odds from `player_prop_odds`
2. **EV bucket conversion** — `roi_by_ev_bucket()` converts `ev_pct` (decimal) to percentage points before comparison with bucket thresholds
3. **Confidence is additive weighted** — pure weighted sum, no interaction terms, no ML. Fully transparent.
4. **Calibration is advisory** — never auto-changes thresholds. Human review required.
5. **Weights are configurable** — `CONFIDENCE_WEIGHTS` in `prop_config.py` allows tuning without code changes

## Verdict

Phase 9 is **COMPLETE**. The intelligence layer provides comprehensive analytics, confidence scoring, and report generation. All 723 tests pass with zero regressions. The system can now answer calibration questions ("is +2% EV profitable?") from historical data and produce sportsbook quality rankings.
